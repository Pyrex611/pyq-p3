"""
p4_utils.py
Shared utilities: UKF model factory, data download, signal generation,
position management, and PositionGuard monitoring service.

CRITICAL API NOTE (2025-12-09):
  Binance migrated all conditional orders (STOP_MARKET, TAKE_PROFIT_MARKET,
  STOP, TAKE_PROFIT, TRAILING_STOP_MARKET) to the Algo Service.
  Old endpoint: POST /fapi/v1/order      → -4120 STOP_ORDER_SWITCH_ALGO
  New endpoint: POST /fapi/v1/algoOrder  → works, returns algoId (not orderId)
  Cancel:       DELETE /fapi/v1/algoOrder with algoId
"""
from __future__ import annotations

import os, json, math, time, logging, hashlib, hmac, threading, traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_FLOOR
from pathlib import Path
from queue import Queue
from typing import Dict, Any, Set, Tuple

import numpy as np
import pandas as pd
import requests
import asyncio
import nest_asyncio
import schedule as _schedule_module

from dotenv import load_dotenv
import datetime as dt

import alpaca_trade_api as tradeapi
from alpaca_trade_api.stream import Stream
from alpaca_trade_api.common import URL
from alpaca.trading.client import TradingClient
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.live.crypto import CryptoDataStream

from pybit.unified_trading import HTTP
from binance.client import Client
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError
from filterpy.kalman import UnscentedKalmanFilter, MerweScaledSigmaPoints
from filterpy.common import Q_discrete_white_noise
from sklearn.metrics import mean_absolute_error, mean_squared_error, mean_absolute_percentage_error, r2_score

import pyquant_signal_math as signal_math
import pyquant_state as state_store
import pyquant_resilience as resilience

nest_asyncio.apply()

# ── Environment ───────────────────────────────────────────────────────────────
_ENV_PATH = Path(__file__).resolve().parent / ".env"
if not _ENV_PATH.exists():
    raise FileNotFoundError(
        f"\n{'='*60}\n  .env file not found at: {_ENV_PATH}\n"
        f"  Copy env_template.txt to .env and fill in your credentials.\n{'='*60}\n"
    )
load_dotenv(dotenv_path=_ENV_PATH, override=True)


def _require_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise EnvironmentError(
            f"\n{'='*60}\n  Required variable not set: {key}\n"
            f"  File: {_ENV_PATH}\n{'='*60}\n"
        )
    return val


ALPACA_API_KEY     = _require_env("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = _require_env("ALPACA_SECRET_KEY")
BYBIT_API_KEY      = _require_env("BYBIT_API_KEY")
BYBIT_SECRET_KEY   = _require_env("BYBIT_SECRET_KEY")
BINANCE_API_KEY    = _require_env("BINANCE_API_KEY")
BINANCE_SECRET_KEY = _require_env("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN     = _require_env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = _require_env("TELEGRAM_CHAT_ID")

# ── Phase 1: paper mode ──────────────────────────────────────────────────────
# PYQUANT_MODE=paper runs against Binance Futures TESTNET — real exchange
# API integration (auth, order placement, Algo Order bracket orders, fills)
# exercised end-to-end with no real capital at risk. This is deliberately
# NOT the same thing as walkforward.py: walkforward validates the STRATEGY
# against history; paper mode validates the live EXECUTION PATH against a
# real (test) exchange. Separate testnet credentials are required —
# mainnet keys do not work against testnet.binancefuture.com and vice
# versa. Defaults to "live" so existing deployments are unaffected.
PYQUANT_MODE = os.environ.get("PYQUANT_MODE", "live").strip().lower()
if PYQUANT_MODE not in ("live", "paper"):
    raise EnvironmentError(f"PYQUANT_MODE must be 'live' or 'paper', got: {PYQUANT_MODE!r}")
IS_PAPER_MODE = (PYQUANT_MODE == "paper")

if IS_PAPER_MODE:
    _BINANCE_KEY    = _require_env("BINANCE_TESTNET_API_KEY")
    _BINANCE_SECRET = _require_env("BINANCE_TESTNET_SECRET_KEY")
else:
    _BINANCE_KEY    = BINANCE_API_KEY
    _BINANCE_SECRET = BINANCE_SECRET_KEY

_binance_lock = threading.Lock()


def _create_binance_client(max_retries: int = 5, retry_delay: int = 5) -> Client:
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            client = Client(_BINANCE_KEY, _BINANCE_SECRET, testnet=IS_PAPER_MODE)
            client.ping()
            print(f"[Startup] Binance connected on attempt {attempt} "
                  f"({'TESTNET / paper mode' if IS_PAPER_MODE else 'mainnet / live'}).")
            return client
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                print(f"[Startup] Binance attempt {attempt}/{max_retries} failed: {e}. Retrying in {retry_delay}s…")
                time.sleep(retry_delay)
    raise ConnectionError(
        f"\n{'='*60}\n  Cannot connect to Binance after {max_retries} attempts.\n"
        f"  Last error: {last_err}\n"
        f"  Check internet, credentials, API permissions.\n{'='*60}\n"
    ) from last_err


crypto_client = CryptoHistoricalDataClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)
crypto_stream = CryptoDataStream(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)
binance_client = _create_binance_client()
session        = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_SECRET_KEY)

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_ch = logging.StreamHandler(); _ch.setLevel(logging.INFO)
_fh = logging.FileHandler("pyq_p4.log"); _fh.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
_ch.setFormatter(_fmt); _fh.setFormatter(_fmt)
if not logger.handlers:
    logger.addHandler(_ch); logger.addHandler(_fh)


# ── OHLCV aggregation ─────────────────────────────────────────────────────────
class OHLCVAggregator:
    @staticmethod
    def aggregate_ohlcv_data(df: pd.DataFrame, aggregation_minutes: int):
        return aggregate_ohlcv_data(df, aggregation_minutes)


def aggregate_ohlcv_data(df: pd.DataFrame, aggregation_minutes: int):
    df_agg = df.copy()
    rules = {k: v for k, v in {"open": "first", "high": "max", "low": "min",
                                "close": "last", "volume": "sum"}.items()
             if k in df_agg.columns}
    if not rules:
        return None
    try:
        return df_agg.resample(f"{aggregation_minutes}min", level="timestamp").agg(rules).dropna(how="all")
    except Exception as e:
        logger.error(f"aggregate_ohlcv_data: {e}")
        return None


def data_download(timeframe: str, or_symbols):
    tf_map = {
        "1H":  TimeFrame.Hour,
        "15T": TimeFrame(15, TimeFrame.Minute),
        "5T":  TimeFrame(5,  TimeFrame.Minute),
        "1T":  TimeFrame.Minute,
    }
    tf = tf_map.get(timeframe)
    if tf is None:
        raise ValueError(f"data_download: unsupported timeframe '{timeframe}'")
    request_params = CryptoBarsRequest(symbol_or_symbols=or_symbols, timeframe=tf)
    bars = crypto_client.get_crypto_bars(request_params)
    return bars.df


# ── UKF Model Helpers ─────────────────────────────────────────────────────────

def _stabilise_P(ukf, floor: float = 1e-6):
    P = (ukf.P + ukf.P.T) / 2
    for i in range(P.shape[0]):
        if P[i, i] < floor:
            P[i, i] = floor
    ukf.P = P


def _make_ukf(init_val: float, alpha: float, beta: float, kappa: int, P_init: float, Q_scale: float, R_init: float):
    n = 2
    pts = MerweScaledSigmaPoints(n=n, alpha=alpha, beta=beta, kappa=kappa)
    fx  = lambda x, dt: np.array([x[0] + dt * x[1], x[1]])
    hx  = lambda x: np.array([x[0]])
    u   = UnscentedKalmanFilter(dim_x=n, dim_z=1, fx=fx, hx=hx, dt=1, points=pts)
    u.P = np.eye(n) * P_init
    u.Q = Q_discrete_white_noise(dim=n, dt=1, var=0.004) * Q_scale
    u.R = np.eye(1) * R_init
    u.x = np.array([init_val, 0.0])
    return u


def _train_ukf(values: np.ndarray, alpha: float, beta: float, kappa: int, P_init: float, Q_scale: float, R_init: float):
    ukf = _make_ukf(values[0], alpha, beta, kappa, P_init, Q_scale, R_init)
    for z in values:
        ukf.predict(); _stabilise_P(ukf)
        ukf.update(z);  _stabilise_P(ukf)
    ukf.predict(); _stabilise_P(ukf)
    return ukf


def build_ukfs(df: pd.DataFrame, symbol: str) -> Tuple[Any, Any, Any]:
    """
    Builds and trains 3 UKFs (close, high, low) on the provided historical DataFrame.
    Matches the exact UKF configuration and hyperparameters of walkforward.py.
    """
    p = ({'alpha': 0.001, 'beta': 7.0, 'kappa': 0, 'P': 0.001, 'Q': 1.0, 'R': 0.01}
         if symbol == 'BTC' else
         {'alpha': 0.001, 'beta': 4.0, 'kappa': 1, 'P': 0.1,   'Q': 1.0, 'R': 0.01})
    a, b, k, P, Q, R = p['alpha'], p['beta'], p['kappa'], p['P'], p['Q'], p['R']
    return (_train_ukf(df['close'].values, a, b, k, P, Q, R),
            _train_ukf(df['high'].values,  a, b, k, P, Q, R),
            _train_ukf(df['low'].values,   a, b, k, P, Q, R))


def step_ukf(ukf, high_ukf, low_ukf, row: pd.Series) -> Tuple[float, float, float]:
    for u, val in [(ukf, row['close']), (high_ukf, row['high']), (low_ukf, row['low'])]:
        u.update(val); _stabilise_P(u)
        u.predict();   _stabilise_P(u)
    return float(ukf.x[0]), float(high_ukf.x[0]), float(low_ukf.x[0])


class UKFModel:
    def __init__(self, crypto_client, start_date):
        self.crypto_client = crypto_client
        self.start_date    = start_date
        self.aggregator    = OHLCVAggregator()

    @staticmethod
    def calculate_metrics(true_values, predictions):
        return {
            "MAE":  mean_absolute_error(true_values, predictions),
            "RMSE": np.sqrt(mean_squared_error(true_values, predictions)),
            "MAPE": mean_absolute_percentage_error(true_values, predictions),
            "R2":   r2_score(true_values, predictions),
        }

    @staticmethod
    def _stabilise_P(ukf, floor: float = 1e-6):
        _stabilise_P(ukf, floor)

    def create_ukf_models(self, timeframe: str, or_symbols: list):
        symb = "BTC" if or_symbols == ["BTC/USD"] else "ETH"
        tf_map = {"1H": TimeFrame.Hour, "15T": TimeFrame.Minute, "5T": TimeFrame.Minute}
        api_tf = tf_map.get(timeframe)
        if api_tf is None:
            return None, None, None

        request_params = CryptoBarsRequest(symbol_or_symbols=or_symbols, timeframe=api_tf, start=self.start_date)
        raw_df = self.crypto_client.get_crypto_bars(request_params).df

        if timeframe == "1H":
            data = raw_df
        elif timeframe in ("15T", "5T"):
            mins = 15 if timeframe == "15T" else 5
            data = self.aggregator.aggregate_ohlcv_data(raw_df.copy(), mins)
            if data is None or len(data) < 10:
                return None, None, None
            data.dropna(inplace=True)

        return build_ukfs(data, symb)

    @staticmethod
    def ukf_handler(data, ukf, high_ukf, low_ukf):
        return step_ukf(ukf, high_ukf, low_ukf, data)


# ── Equity and Position Helpers ───────────────────────────────────────────────

def get_equities() -> tuple:
    usdt_equity = btc_equity = eth_equity = 0.0
    try:
        with _binance_lock:
            account_info = binance_client.futures_account()
        for asset in account_info["assets"]:
            name = asset["asset"]
            if name == "USDT": usdt_equity = float(asset["walletBalance"])
            elif name == "BTC": btc_equity  = float(asset["walletBalance"])
            elif name == "ETH": eth_equity  = float(asset["walletBalance"])
    except Exception as e:
        logger.error(f"get_equities: {e}")
    return usdt_equity, btc_equity, eth_equity


def check_open_position(symbol: str) -> dict:
    position_info = {
        "is_open_position": False, "symbol": symbol,
        "side": None, "size": 0.0, "entry_price": None,
        "unrealized_pnl": 0.0, "liquidation_price": None,
        "take_profit_price": None, "stop_loss_price": None,
    }
    try:
        with _binance_lock:
            positions = binance_client.futures_position_information()
        usdt_eq, _, _ = get_equities()
        wallet_balance = usdt_eq
        position = next(
            (p for p in positions if p["symbol"] == symbol and float(p["positionAmt"]) != 0),
            None
        )
        if position:
            position_info["is_open_position"] = True
            position_info["side"]             = "BUY" if float(position["positionAmt"]) > 0 else "SELL"
            position_info["size"]             = abs(float(position["positionAmt"]))
            position_info["entry_price"]      = float(position["entryPrice"])
            position_info["unrealized_pnl"]   = float(position["unRealizedProfit"])
            position_info["liquidation_price"] = float(position["liquidationPrice"])
            upnl = position_info["unrealized_pnl"]
            # Profit-target early exit — ported from walkforward.py's validated
            # run (Phase 0 unification). The threshold is a fraction of
            # MARGIN-AT-RISK (the capital actually allocated to this position,
            # derived from its real exchange size/entry), not of the full
            # wallet balance — the previous wallet_balance-relative version
            # (0.2 BTC / 0.15 ETH of wallet_balance, plus a $0.6 absolute
            # floor) was a live-only approximation that drifted from the
            # validated reference.
            pt_threshold = signal_math.profit_target_threshold(
                symbol, position_info["size"], position_info["entry_price"]
            )
            if upnl >= pt_threshold:
                close_futures_position(position_info)
                logger.info(
                    f"check_open_position: profit-target early exit for {symbol} "
                    f"(uPNL=${upnl:.4f} >= margin-at-risk threshold ${pt_threshold:.4f})"
                )
    except Exception as e:
        logger.error(f"check_open_position ({symbol}): {e}")
    return position_info


def close_futures_position(position_data: dict) -> dict:
    try:
        symbol     = position_data["symbol"]
        side       = position_data["side"]
        size       = position_data["size"]
        close_side = "SELL" if side == "BUY" else "BUY"
        with _binance_lock:
            response = binance_client.futures_create_order(
                symbol=symbol, side=close_side,
                type="MARKET", quantity=size, reduceOnly=True
            )
        logger.info(f"close_futures_position: closed {symbol} via {close_side} MARKET.")
        return response
    except Exception as e:
        err = f"close_futures_position ({position_data.get('symbol')}): {e}"
        logger.error(err)
        return {"error": err}


# ── ATR and Signal Generation ─────────────────────────────────────────────────

def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < 2:
        return 0.0
    h = df["high"].values; l = df["low"].values; c = df["close"].values
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]),
                               np.abs(l[1:] - c[:-1])))
    n = min(period, len(tr))
    return float(np.mean(tr[-n:])) if n > 0 else 0.0


def fetch_recent_bars(symbol_or_symbols, hours: int = 20) -> pd.DataFrame | None:
    try:
        start = datetime.now(timezone.utc) - timedelta(hours=hours)
        request_params = CryptoBarsRequest(
            symbol_or_symbols=symbol_or_symbols,
            timeframe=TimeFrame.Hour,
            start=start,
        )
        bars = crypto_client.get_crypto_bars(request_params)
        df = bars.df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol_or_symbols, level="symbol") if isinstance(symbol_or_symbols, str) else df
        return df
    except Exception as e:
        logger.error(f"fetch_recent_bars ({symbol_or_symbols}): {e}")
        return None


class SignalGenerator:
    def __init__(self):
        self.sig_gened = False
        self.signals   = []
        self.signal    = None
        self.sig_vol   = False
        self.stake     = 0.9985
        self.pct_diff  = None
        self.trend     = None

    @staticmethod
    def load_params() -> dict:
        # Phase 1: DB-first (pyquant_state), same reasoning as
        # PositionGuard's monitored_orders — optimal_params.json on local
        # disk doesn't survive a stateless Vercel invocation, and the
        # daily optimizer (p4_orchestra.py) now writes to the DB too.
        db_params = state_store.get_json("optimal_params")
        if db_params is not None:
            return db_params
        try:
            with open("optimal_params.json", "r") as f:
                params = json.load(f)
            state_store.set_json("optimal_params", params)  # seed the DB from the legacy file
            return params
        except FileNotFoundError:
            return {"strict_btc": 0.0025, "stricts_btc": 0.005,
                    "strict_eth": 0.0035, "stricts_eth": 0.0075,
                    "atr_mult": 1.8}

    def _calculate_tp_price(self, price, pred, rounds, leverage, bal, tp_inc, entry, trend):
        # NOTE: this function previously also took a `tp_multiplier` arg
        # (fed from `_get_symbol_params()`'s now-removed buy_tp/sell_tp
        # keys). It was never referenced in the body — only `tp_inc`
        # actually drove the TP price, in both this file and
        # walkforward.py's own generate_signal(). Removed as dead
        # plumbing (Phase 0 cleanup); TP behavior is unchanged.
        net_bal = bal * tp_inc
        net_val = net_bal * leverage
        tp_qty  = (bal * leverage) / entry if entry > 0 else 1.0
        tp_candidate = net_val / tp_qty if tp_qty > 0 else pred
        if trend == "up":
            return round(tp_candidate, rounds) if pred > tp_candidate else round(pred, rounds)
        return round(tp_candidate, rounds) if pred < tp_candidate else round(pred, rounds)

    def _calculate_sl_price(self, entry_price, symbol, trend, atr=0.0, atr_mult=None):
        is_btc = (symbol in ("BTC/USD", "BTC", "BTCUSDT"))
        rounds = 0 if is_btc else 2
        # SL delta/floor now come from the shared module (Phase 0
        # unification) so this always matches walkforward.py's sl_price().
        delta = signal_math.sl_delta(symbol, atr, atr_mult)

        if trend == "up":
            return round(entry_price - delta, rounds)
        return round(entry_price + delta, rounds)

    def _get_symbol_params(self, symbol: str, params: dict) -> dict:
        # buy_tp/sell_tp removed (Phase 0 cleanup) — they were never read
        # by `_calculate_tp_price()`, in this file or in walkforward.py's
        # own generate_signal(); only tp_inc/sell_inc actually drive TP.
        if symbol in ("BTC/USD", "BTCUSDT"):
            tp_inc = 1.004
            return {
                "strict": params.get("strict_btc", 0.0025),
                "trade_symbol": "BTCUSDT",
                "pct_diff_and_vol_strict": params.get("stricts_btc", 0.005),
                "rounds": 0, "tp_inc": tp_inc, "sell_inc": 2 - tp_inc,
            }
        elif symbol in ("ETH/USD", "ETHUSDT"):
            tp_inc = 1.05
            return {
                "strict": params.get("strict_eth", 0.0035),
                "trade_symbol": "ETHUSDT",
                "pct_diff_and_vol_strict": params.get("stricts_eth", 0.0075),
                "rounds": 2, "tp_inc": tp_inc, "sell_inc": 2 - tp_inc,
            }
        raise ValueError(f"Unknown symbol: {symbol}")

    def generate_signal(self, price, pred, high_pred, low_pred, symbol, leverage, bal,
                        atr=0.0, atr_mult=None):
        price, pred = float(price), float(pred)
        high_pred, low_pred = float(high_pred), float(low_pred)

        self.signal = None; self.trend = None
        if pred > price:
            trend = self.trend = "up"
            pct_diff = abs(1 - pred / price)
            vol = abs(1 - high_pred / pred) if pred > 0 else 0
        elif price > pred:
            trend = self.trend = "down"
            pct_diff = abs(1 - price / pred)
            vol = abs(1 - pred / low_pred) if low_pred > 0 else 0
        else:
            return None

        self.pct_diff = float(pct_diff)
        params = self.load_params()
        try:
            sp = self._get_symbol_params(symbol, params)
        except ValueError:
            return None

        self.sig_vol = (pct_diff <= sp["pct_diff_and_vol_strict"]) or (vol >= sp["pct_diff_and_vol_strict"])
        if not self.sig_vol:
            return None

        r = sp["rounds"]
        if trend == "up":
            buy_price = low_pred
            if buy_price <= 0 or not (pred > buy_price and abs(1 - pred / buy_price) >= sp["strict"]):
                return None
            entry = round(price if price < buy_price else buy_price, r)
            order_type = "Market" if price < buy_price else "Limit"
            tp_price = self._calculate_tp_price(price, pred, r, leverage, bal, sp["tp_inc"], entry, "up")
            sl_price = self._calculate_sl_price(entry, symbol, "up", atr, atr_mult)
            # NOTE: `bal` here is the wallet balance at signal-generation
            # time, before hourly_task() knows whether this will end up a
            # single-signal (80%-of-balance) or double-signal (50/50) trade.
            # This figure is therefore provisional; pyq_p4.py recalculates
            # it against the actual allocated trade capital once that split
            # is known, via signal_math.expected_max_loss() (Phase 0 fix).
            expected_max_loss = signal_math.expected_max_loss(entry, sl_price, bal, leverage)

            sig = {"symbol": sp["trade_symbol"], "order_type": order_type,
                   "entry_price": float(entry), "order_side": "Buy",
                   "tp_price": float(tp_price), "sl_price": float(sl_price),
                   "current_price": float(price), "checked": False, "current_bal": float(bal),
                   "atr": float(atr), "atr_mult": float(atr_mult or 1.8),
                   "expected_max_loss": float(expected_max_loss)}
            self.sig_gened = True
            self.signal = sig
            self.signals.append(sig)
            return sig

        elif trend == "down":
            sell_price = high_pred
            if sell_price <= 0 or not (sell_price > pred and abs(1 - sell_price / pred) >= sp["strict"]):
                return None
            entry = round(price if price > sell_price else sell_price, r)
            order_type = "Market" if price > sell_price else "Limit"
            tp_price = self._calculate_tp_price(price, pred, r, leverage, bal, sp["sell_inc"], entry, "down")
            sl_price = self._calculate_sl_price(entry, symbol, "down", atr, atr_mult)
            # Provisional figure — see the matching NOTE in the "up" branch
            # above; pyq_p4.py recalculates this once the actual allocated
            # trade capital is known (Phase 0 fix).
            expected_max_loss = signal_math.expected_max_loss(entry, sl_price, bal, leverage)

            sig = {"symbol": sp["trade_symbol"], "order_type": order_type,
                   "entry_price": float(entry), "order_side": "Sell",
                   "tp_price": float(tp_price), "sl_price": float(sl_price),
                   "current_price": float(price), "checked": False, "current_bal": float(bal),
                   "atr": float(atr), "atr_mult": float(atr_mult or 1.8),
                   "expected_max_loss": float(expected_max_loss)}
            self.sig_gened = True
            self.signal = sig
            self.signals.append(sig)
            return sig

        return None


# ── PositionGuard ─────────────────────────────────────────────────────────────

class PositionGuard:
    def __init__(self, binance_client: Client, telegram_token: str, chat_id: str):
        self.binance          = binance_client
        self.telegram_token   = telegram_token
        self.telegram_chat_id = chat_id
        self.telegram_app     = None

        self.trade_state_file = "active_trades.json"
        self.active_trades    = self.load_trade_state()
        self.monitored_orders: Dict[str, Dict[str, Any]] = self.load_trade_state()
        self.monitored_positions: Set[str] = set()
        self.alert_queue      = Queue()

        self.running          = False
        self.scheduler_thread = None
        self.telegram_thread  = None

        self._signal_store: Dict[str, Any] = {}
        self._bracket_placed_this_tick: Set[str] = set()
        self._bracket_orders: Dict[str, Dict[str, Any]] = {}
        self.slippage_log_file = "slippage_log.jsonl"
        self._api_lock = _binance_lock
        self._hedge_mode: bool = self._detect_hedge_mode()

    def load_trade_state(self) -> dict:
        # Phase 1: primary store is pyquant_state (SQLite locally, Postgres
        # in production) so this survives a stateless Vercel invocation.
        # The local active_trades.json file is read as a one-time migration
        # fallback only — if the DB has nothing yet but the file does, that
        # means this is the first run after upgrading, so seed the DB from
        # it and use the file's content for this call.
        db_state = state_store.get_json("position_guard_monitored_orders")
        if db_state is not None:
            return db_state
        if os.path.exists(self.trade_state_file):
            try:
                with open(self.trade_state_file, "r") as f:
                    legacy_state = json.load(f)
                state_store.set_json("position_guard_monitored_orders", legacy_state)
                return legacy_state
            except Exception:
                return {}
        return {}

    def save_to_json(self):
        # Despite the name (kept for call-site compatibility), this now
        # writes to pyquant_state, not JSON on disk (Phase 1). A local
        # file write is also attempted, best-effort, so a VPS/orchestra
        # deployment with no DATABASE_URL set still has a human-readable
        # active_trades.json to inspect — but the DB write is the one that
        # actually matters for correctness.
        try:
            state_store.set_json("position_guard_monitored_orders", self.monitored_orders)
        except Exception as e:
            logger.error(f"PositionGuard.save_to_json (state store): {e}")
        try:
            with open(self.trade_state_file, "w") as f:
                json.dump(self.monitored_orders, f, indent=4, default=str)
        except Exception:
            pass  # best-effort only; the state-store write above is authoritative

    def _detect_hedge_mode(self, max_retries: int = 5, retry_delay: int = 3) -> bool:
        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                with self._api_lock:
                    result = self.binance.futures_get_position_mode()
                mode = result.get("dualSidePosition", False)
                logger.info(f"[PositionGuard] Mode confirmed (attempt {attempt}): "
                            f"{'HEDGE' if mode else 'ONE-WAY'}")
                return mode
            except Exception as e:
                last_err = e
                if attempt < max_retries:
                    logger.warning(f"[PositionGuard] Mode detection attempt {attempt}/{max_retries}: {e}. Retrying…")
                    time.sleep(retry_delay)
        raise ConnectionError(f"[PositionGuard] Cannot confirm position mode: {last_err}")

    def log_slippage(self, symbol: str, leg: str, side: str, trigger_price: float, fill_price: float, order_id=None):
        try:
            if not trigger_price or not fill_price:
                return
            is_buy = str(side).upper() == "BUY"
            signed_diff = (fill_price - trigger_price) if is_buy else (trigger_price - fill_price)
            slip_pct = (signed_diff / trigger_price) * 100
            record = {
                "timestamp": datetime.now().isoformat(), "symbol": symbol,
                "leg": leg, "side": side, "order_id": order_id,
                "trigger_price": trigger_price, "fill_price": fill_price,
                "slippage_abs": signed_diff, "slippage_pct": slip_pct,
            }
            with open(self.slippage_log_file, "a") as f:
                f.write(json.dumps(record) + "\n")
            logger.info(f"📊 Slippage [{leg}] {symbol}: trigger={trigger_price} fill={fill_price} slip={slip_pct:+.4f}%")
        except Exception as e:
            logger.error(f"log_slippage ({symbol}, {leg}): {e}")

    async def send_telegram_alert(self, message: str):
        try:
            if self.telegram_app and self.telegram_app.bot:
                await self.telegram_app.bot.send_message(
                    chat_id=self.telegram_chat_id, text=message)
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    def add_alert_to_queue(self, message: str):
        self.alert_queue.put(message)

    async def process_alert_queue(self):
        while self.running:
            try:
                if not self.alert_queue.empty():
                    await self.send_telegram_alert(self.alert_queue.get_nowait())
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"process_alert_queue: {e}")
                await asyncio.sleep(1)

    async def status_command(self, update: Update, context):
        if update.message:
            await update.message.reply_text(
                f"🔄 PositionGuard\nOrders: {len(self.monitored_orders)}\n"
                f"Positions: {len(self.monitored_positions)}\nRunning: {self.running}")

    async def list_command(self, update: Update, context):
        msg = "📋 Monitored\n"
        for oid, info in self.monitored_orders.items():
            msg += f"  {info['symbol']} (ID:{oid})\n"
        if update.message:
            await update.message.reply_text(msg or "Nothing monitored.")

    async def stop_command(self, update: Update, context):
        self.running = False
        if update.message:
            await update.message.reply_text("🛑 Stopping…")

    def check_all_orders_and_positions(self):
        if not self.monitored_orders and not self.monitored_positions and not self._bracket_orders:
            return

        self._bracket_placed_this_tick.clear()
        orders_to_remove = []

        for order_id, order_info in list(self.monitored_orders.items()):
            try:
                symbol = order_info["symbol"]
                with self._api_lock:
                    order_status = self.binance.futures_get_order(symbol=symbol, orderId=int(order_id))
                status = order_status.get("status")
                order_info["status"] = status

                if status == "FILLED":
                    self.add_alert_to_queue(f"✅ Order {order_id} FILLED for {symbol}.")
                    sig = order_info.get("signal")
                    if sig:
                        self._signal_store[symbol] = sig
                        try:
                            avg_price = float(order_status.get("avgPrice", 0))
                            trigger   = float(sig.get("entry_price", 0))
                            if avg_price > 0:
                                self.log_slippage(symbol, "ENTRY", sig.get("order_side", "?"), trigger, avg_price, order_id)
                        except Exception as slip_err:
                            logger.error(f"Entry slippage logging ({symbol}): {slip_err}")
                    orders_to_remove.append(order_id)
                    self._check_and_place_bracket(symbol, order_id=order_id, signal_override=sig)

                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    self.add_alert_to_queue(f"❌ Order {order_id} failed: {status}.")
                    orders_to_remove.append(order_id)
                    self._signal_store.pop(symbol, None)

            except Exception as e:
                logger.error(f"Error checking order {order_id}: {e}")

        for oid in orders_to_remove:
            self.monitored_orders.pop(oid, None)
        if orders_to_remove:
            self.save_to_json()

        for symbol in list(self.monitored_positions):
            if symbol in self._bracket_placed_this_tick:
                continue
            try:
                with self._api_lock:
                    open_algos = self.binance._request_futures_api(
                        'get', 'algoOrders/current', signed=True, data={"symbol": symbol})
                has_bracket = bool(open_algos.get("orders"))
                if has_bracket:
                    self.add_alert_to_queue(f"🛡️ {symbol} has active algo TP/SL.")
                    self.monitored_positions.discard(symbol)
                    self._signal_store.pop(symbol, None)
                else:
                    with self._api_lock:
                        positions = self.binance.futures_position_information(symbol=symbol)
                    position = next((p for p in positions if float(p["positionAmt"]) != 0), None)
                    if not position:
                        self.monitored_positions.discard(symbol)
                        self._signal_store.pop(symbol, None)
                    else:
                        self._check_and_place_bracket(symbol, signal_override=self._signal_store.get(symbol))
            except Exception as e:
                logger.error(f"Error checking position for {symbol}: {e}")

        self._check_bracket_fills()

    def _check_and_place_bracket(self, symbol: str, order_id=None, signal_override=None):
        self._bracket_placed_this_tick.add(symbol)
        try:
            with self._api_lock:
                positions = self.binance.futures_position_information(symbol=symbol)
            live_pos = next((p for p in positions if float(p["positionAmt"]) != 0), None)
            if not live_pos:
                self.monitored_positions.discard(symbol)
                self._signal_store.pop(symbol, None)
                return

            signal = signal_override or self._signal_store.get(symbol)
            if not signal:
                self._place_bracket_order_fallback(symbol, live_pos)
                return

            resp = self._place_bracket_order(symbol, live_pos, signal)
            if resp.get("sl_order") or resp.get("tp_order"):
                self.monitored_positions.discard(symbol)
                self._signal_store.pop(symbol, None)

        except Exception as e:
            logger.error(f"_check_and_place_bracket fatal ({symbol}): {e}")

    def _place_bracket_order(self, symbol: str, position_info: dict, signal: dict) -> dict:
        sl_order = tp_order = None
        try:
            position_amt = float(position_info.get("positionAmt", position_info.get("size", 0)))
            is_long    = position_amt > 0
            close_side = "SELL" if is_long else "BUY"

            sl_price = float(signal["sl_price"])
            tp_price = float(signal["tp_price"])

            with self._api_lock:
                info = self.binance.futures_exchange_info()
            symbol_info = next(s for s in info["symbols"] if s["symbol"] == symbol)
            pf = next(f for f in symbol_info["filters"] if f["filterType"] == "PRICE_FILTER")
            lf = next(f for f in symbol_info["filters"] if f["filterType"] == "LOT_SIZE")

            tick_size  = float(pf["tickSize"])
            step_size  = float(lf["stepSize"])
            ts_str     = str(tick_size)
            ss_str     = str(step_size)
            price_prec = len(ts_str.split(".")[1].rstrip("0")) if "." in ts_str else 0
            qty_prec   = len(ss_str.split(".")[1].rstrip("0")) if "." in ss_str else 0

            sl_price = round(round(sl_price / tick_size) * tick_size, price_prec)
            tp_price = round(round(tp_price / tick_size) * tick_size, price_prec)

            entry = float(position_info.get("entry_price") or position_info.get("entryPrice", 0))
            if entry > 0:
                if is_long:
                    if sl_price >= entry: sl_price = round(round(entry * 0.995 / tick_size) * tick_size, price_prec)
                    if tp_price <= entry: tp_price = round(round(entry * 1.005 / tick_size) * tick_size, price_prec)
                else:
                    if sl_price <= entry: sl_price = round(round(entry * 1.005 / tick_size) * tick_size, price_prec)
                    if tp_price >= entry: tp_price = round(round(entry * 0.995 / tick_size) * tick_size, price_prec)

            sl_str  = f"{sl_price:.{price_prec}f}"
            tp_str  = f"{tp_price:.{price_prec}f}"
            qty_str = f"{abs(position_amt):.{qty_prec}f}"
            pos_side = ("LONG" if is_long else "SHORT") if self._hedge_mode else "BOTH"

            def _algo_params(order_type: str, trigger_price: str) -> dict:
                params = {
                    "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side,
                    "type": order_type, "triggerPrice": trigger_price,
                    "quantity": qty_str, "workingType": "MARK_PRICE", "positionSide": pos_side,
                }
                if not self._hedge_mode:
                    params["reduceOnly"] = "true"
                return params

            def _existing_order_of_type(order_type: str) -> bool:
                with self._api_lock:
                    current = self.binance._request_futures_api(
                        'get', 'algoOrders/current', signed=True, data={"symbol": symbol})
                return any(o.get("type") == order_type for o in (current or []))

            def _place_sl():
                with self._api_lock:
                    return self.binance._request_futures_api(
                        'post', 'algoOrder', signed=True, data=_algo_params("STOP_MARKET", sl_str))

            def _place_tp():
                with self._api_lock:
                    return self.binance._request_futures_api(
                        'post', 'algoOrder', signed=True, data=_algo_params("TAKE_PROFIT_MARKET", tp_str))

            # Phase 1: retry/backoff on transient failures, with a
            # duplicate-order guard before any retry (not the first
            # attempt) — see pyquant_resilience.py's module docstring for
            # why that matters on an endpoint with no client-order-id.
            sl_order = resilience.retry_algo_order(
                _place_sl, lambda: _existing_order_of_type("STOP_MARKET"),
                description=f"_place_bracket_order SL ({symbol})")
            tp_order = resilience.retry_algo_order(
                _place_tp, lambda: _existing_order_of_type("TAKE_PROFIT_MARKET"),
                description=f"_place_bracket_order TP ({symbol})")

        except Exception as e:
            logger.error(f"_place_bracket_order ({symbol}): {e}")

        if sl_order and tp_order:
            self._bracket_orders[symbol] = {
                "sl_algo_id": sl_order.get("algoId"), "tp_algo_id": tp_order.get("algoId"),
                "sl_trigger": float(sl_str), "tp_trigger": float(tp_str),
                "side": "BUY" if is_long else "SELL",
            }

        return {"sl_order": sl_order, "tp_order": tp_order}

    def _place_bracket_order_fallback(self, symbol: str, position_info: dict) -> tuple:
        try:
            position_amt = float(position_info.get("positionAmt", position_info.get("size", 0)))
            is_long    = position_amt > 0
            close_side = "SELL" if is_long else "BUY"
            entry_price = float(position_info.get("entry_price") or position_info.get("entryPrice", 0))

            tp_price = entry_price * 1.005 if is_long else entry_price * 0.995
            sl_price = entry_price * 0.995 if is_long else entry_price * 1.005

            with self._api_lock:
                info = self.binance.futures_exchange_info()
            symbol_info = next(s for s in info["symbols"] if s["symbol"] == symbol)
            pf = next(f for f in symbol_info["filters"] if f["filterType"] == "PRICE_FILTER")
            lf = next(f for f in symbol_info["filters"] if f["filterType"] == "LOT_SIZE")
            tick_size  = float(pf["tickSize"])
            step_size  = float(lf["stepSize"])
            ts_str     = str(tick_size); ss_str = str(step_size)
            price_prec = len(ts_str.split(".")[1].rstrip("0")) if "." in ts_str else 0
            qty_prec   = len(ss_str.split(".")[1].rstrip("0")) if "." in ss_str else 0

            sl_str   = f"{round(round(sl_price / tick_size) * tick_size, price_prec):.{price_prec}f}"
            tp_str   = f"{round(round(tp_price / tick_size) * tick_size, price_prec):.{price_prec}f}"
            qty_str  = f"{abs(position_amt):.{qty_prec}f}"
            pos_side = ("LONG" if is_long else "SHORT") if self._hedge_mode else "BOTH"

            def _algo_params(order_type, trigger_price):
                p = {"algoType": "CONDITIONAL", "symbol": symbol, "side": close_side,
                     "type": order_type, "triggerPrice": trigger_price,
                     "quantity": qty_str, "workingType": "MARK_PRICE", "positionSide": pos_side}
                if not self._hedge_mode: p["reduceOnly"] = "true"
                return p

            def _existing_order_of_type(order_type: str) -> bool:
                with self._api_lock:
                    current = self.binance._request_futures_api(
                        'get', 'algoOrders/current', signed=True, data={"symbol": symbol})
                return any(o.get("type") == order_type for o in (current or []))

            def _place_sl():
                with self._api_lock:
                    return self.binance._request_futures_api(
                        'post', 'algoOrder', signed=True, data=_algo_params("STOP_MARKET", sl_str))

            def _place_tp():
                with self._api_lock:
                    return self.binance._request_futures_api(
                        'post', 'algoOrder', signed=True, data=_algo_params("TAKE_PROFIT_MARKET", tp_str))

            # Phase 1: same retry/backoff + duplicate-order guard as the
            # primary placement path above.
            fallback_sl = resilience.retry_algo_order(
                _place_sl, lambda: _existing_order_of_type("STOP_MARKET"),
                description=f"_place_bracket_order_fallback SL ({symbol})")
            fallback_tp = resilience.retry_algo_order(
                _place_tp, lambda: _existing_order_of_type("TAKE_PROFIT_MARKET"),
                description=f"_place_bracket_order_fallback TP ({symbol})")

            return fallback_tp, fallback_sl
        except Exception as e:
            logger.error(f"_place_bracket_order_fallback ({symbol}): {e}")
            return None, None

    def _check_bracket_fills(self):
        for symbol in list(self._bracket_orders.keys()):
            pair = self._bracket_orders[symbol]
            try:
                with self._api_lock:
                    positions = self.binance.futures_position_information(symbol=symbol)
                still_open = any(float(p["positionAmt"]) != 0 for p in positions)
                if still_open:
                    continue

                with self._api_lock:
                    trades = self.binance.futures_account_trades(symbol=symbol, limit=5)

                fill_price = float(trades[-1]["price"]) if trades else None
                if fill_price:
                    side = pair["side"]
                    sl_trig, tp_trig = pair["sl_trigger"], pair["tp_trigger"]
                    leg = "SL" if (fill_price <= sl_trig * 1.002 if side == "BUY" else fill_price >= sl_trig * 0.998) else "TP"
                    sibling_id = pair["tp_algo_id"] if leg == "SL" else pair["sl_algo_id"]
                    self.log_slippage(symbol, leg, side, sl_trig if leg == "SL" else tp_trig, fill_price)
                    self._cancel_sibling_algo_order(sibling_id, symbol, "TP" if leg == "SL" else "SL")

                del self._bracket_orders[symbol]
            except Exception as e:
                logger.error(f"_check_bracket_fills ({symbol}): {e}")
                self._bracket_orders.pop(symbol, None)

    def _cancel_sibling_algo_order(self, algo_id, symbol: str, leg_name: str):
        if not algo_id: return
        try:
            def _cancel():
                with self._api_lock:
                    return self.binance._request_futures_api(
                        'delete', 'algoOrder', signed=True, data={"algoId": algo_id})
            # DELETE is naturally idempotent (cancelling an already-cancelled
            # order just errors harmlessly), so a plain retry is safe here —
            # no duplicate-order guard needed (Phase 1).
            resilience.retry_api_call(
                _cancel, description=f"_cancel_sibling_algo_order {leg_name} ({symbol})")
            logger.info(f"Cancelled dangling {leg_name} algo order {algo_id} for {symbol}.")
        except Exception as e:
            logger.info(f"Could not cancel {leg_name} algo order {algo_id} ({symbol}): {e}")

    def start_guard_for_order(self, signal: Dict[str, Any], order_id: int):
        symbol = signal["symbol"]
        self.monitored_orders[str(order_id)] = {
            "symbol": symbol, "signal": signal,
            "status": "NEW", "added_time": datetime.now().isoformat(),
        }
        self.save_to_json()
        msg = f"👮 PositionGuard monitoring entry order {order_id} for {symbol}."
        self.add_alert_to_queue(msg)
        logger.info(msg)

    async def run_telegram_bot(self):
        _app_started = False
        try:
            self.telegram_app = Application.builder().token(self.telegram_token).build()
            self.telegram_app.add_handler(CommandHandler("status", self.status_command))
            self.telegram_app.add_handler(CommandHandler("list",   self.list_command))
            self.telegram_app.add_handler(CommandHandler("stop",   self.stop_command))
            await self.telegram_app.initialize()

            # FIX FOR CONFLICT ERROR: Delete active webhook before starting polling
            try:
                await self.telegram_app.bot.delete_webhook(drop_pending_updates=True)
                logger.info("[Telegram] Successfully deleted webhook before starting polling.")
            except Exception as wh_err:
                logger.warning(f"[Telegram] Webhook deletion note: {wh_err}")

            await self.telegram_app.start()
            _app_started = True
            await self.telegram_app.updater.start_polling(drop_pending_updates=True)
            await self.process_alert_queue()
        except Exception as e:
            logger.error(f"Telegram bot error: {e}")
        finally:
            if _app_started and self.telegram_app is not None:
                try:
                    if self.telegram_app.updater and self.telegram_app.updater.running:
                        await self.telegram_app.updater.stop()
                    await self.telegram_app.stop()
                    await self.telegram_app.shutdown()
                except Exception as stop_err:
                    logger.error(f"Telegram bot shutdown error: {stop_err}")

    def run_scheduler(self):
        private_sched = _schedule_module.Scheduler()
        private_sched.every(30).seconds.do(self.check_all_orders_and_positions)
        self.add_alert_to_queue("🔔 PositionGuard scheduler ONLINE.")
        logger.info("PositionGuard scheduler started (private scheduler instance).")
        while self.running:
            private_sched.run_pending()
            time.sleep(1)

    def start(self):
        self.running = True
        self.scheduler_thread = threading.Thread(target=self.run_scheduler, daemon=True)
        self.scheduler_thread.start()

        def _run_telegram():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.run_telegram_bot())

        self.telegram_thread = threading.Thread(target=_run_telegram, daemon=True)
        self.telegram_thread.start()

    def stop(self):
        self.running = False
        self.add_alert_to_queue("🛑 PositionGuard stopped.")
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=5)
        if self.telegram_thread:
            self.telegram_thread.join(timeout=5)