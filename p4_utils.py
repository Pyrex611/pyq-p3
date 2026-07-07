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
  All bracket order placement in this file uses the new endpoint exclusively.
"""

import os, json, math, time, logging, hashlib, hmac, threading, traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_FLOOR
from pathlib import Path
from queue import Queue
from typing import Dict, Any, Set

import numpy as np
import pandas as pd
import requests
import asyncio
import nest_asyncio
import schedule as _schedule_module  # private instances only — never use global

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

# Shared threading lock — python-binance Client is not thread-safe.
# Every Binance API call in this codebase acquires this lock first.
_binance_lock = threading.Lock()


def _create_binance_client(max_retries: int = 5, retry_delay: int = 5) -> Client:
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, testnet=False)
            client.ping()
            print(f"[Startup] Binance connected on attempt {attempt}.")
            return client
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                print(f"[Startup] Binance attempt {attempt}/{max_retries} failed: {e}. Retrying in {retry_delay}s…")
                time.sleep(retry_delay)
    raise ConnectionError(
        f"\n{'='*60}\n  Cannot connect to Binance after {max_retries} attempts.\n"
        f"  Last error: {last_err}\n"
        f"  Check: internet, api.binance.com:443, .env credentials, IP ban.\n{'='*60}\n"
    ) from last_err


# Shared clients — created once, imported by pyq_p3.py to avoid duplicate connections.
start_date    = dt.date.today() - dt.timedelta(days=60)
end_date      = dt.date.today()
crypto_client = CryptoHistoricalDataClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)
crypto_stream = CryptoDataStream(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)
binance_client = _create_binance_client()
session        = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_SECRET_KEY)

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_ch = logging.StreamHandler(); _ch.setLevel(logging.INFO)
_fh = logging.FileHandler("pyq_p3.log"); _fh.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
_ch.setFormatter(_fmt); _fh.setFormatter(_fmt)
if not logger.handlers:
    logger.addHandler(_ch); logger.addHandler(_fh)


# ── OHLCV aggregation ─────────────────────────────────────────────────────────
class OHLCVAggregator:
    @staticmethod
    def aggregate_ohlcv_data(df, aggregation_minutes: int):
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


# ── Live data download ────────────────────────────────────────────────────────
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


# ── UKF model ─────────────────────────────────────────────────────────────────
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
        P = (ukf.P + ukf.P.T) / 2
        for i in range(P.shape[0]):
            if P[i, i] < floor:
                P[i, i] = floor
        ukf.P = P

    def create_ukf_models(self, timeframe: str, or_symbols: list):
        dt_step, n_dim_state, n_dim_meas = 1, 2, 1
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

        best_params = (
            {"alpha": 0.001, "beta": 4.0, "kappa": 1, "P": 0.1,   "Q": 1.0, "R": 0.01}
            if symb == "ETH" else
            {"alpha": 0.001, "beta": 7.0, "kappa": 0, "P": 0.001, "Q": 1.0, "R": 0.01}
        )
        alpha, beta, kappa = best_params["alpha"], best_params["beta"], best_params["kappa"]
        P, Q, R = best_params["P"], best_params["Q"], best_params["R"]
        points = MerweScaledSigmaPoints(n=n_dim_state, alpha=alpha, beta=beta, kappa=kappa)

        def fx(x, dt): return np.array([x[0] + dt * x[1], x[1]])
        def hx(x):     return np.array([x[0]])

        close_prices = data["close"].values
        high_prices  = data["high"].values
        low_prices   = data["low"].values
        train_size = int(len(close_prices) * 0.7)
        train_close = close_prices[:train_size]; test_close = close_prices[train_size:]
        train_high  = high_prices[:train_size];  test_high  = high_prices[train_size:]
        train_low   = low_prices[:train_size];   test_low   = low_prices[train_size:]

        def _make_ukf(init_val):
            u = UnscentedKalmanFilter(dim_x=n_dim_state, dim_z=n_dim_meas, fx=fx, hx=hx, dt=dt_step, points=points)
            u.P = np.eye(n_dim_state) * P
            u.Q = Q_discrete_white_noise(dim=n_dim_state, dt=dt_step, var=0.004) * Q
            u.R = np.eye(n_dim_meas) * R
            u.x = np.array([init_val, 0.0])
            return u

        def _train(values, ukf_inst):
            preds = []
            for z in values:
                ukf_inst.predict(); self._stabilise_P(ukf_inst)
                preds.append(ukf_inst.x[0])
                ukf_inst.update(z); self._stabilise_P(ukf_inst)
            return preds

        ukf      = _make_ukf(train_close[0])
        high_ukf = _make_ukf(train_high[0])
        low_ukf  = _make_ukf(train_low[0])

        tr_p  = _train(train_close, ukf);  te_p  = _train(test_close, ukf)
        tr_hp = _train(train_high,  high_ukf); te_hp = _train(test_high, high_ukf)
        tr_lp = _train(train_low,   low_ukf);  te_lp = _train(test_low,  low_ukf)

        metrics = {
            "Metric":     ["MAE", "RMSE", "MAPE", "R2"],
            "Train":      list(self.calculate_metrics(train_close, tr_p).values()),
            "Test":       list(self.calculate_metrics(test_close,  te_p).values()),
            "High Train": list(self.calculate_metrics(train_high,  tr_hp).values()),
            "High Test":  list(self.calculate_metrics(test_high,   te_hp).values()),
            "Low Train":  list(self.calculate_metrics(train_low,   tr_lp).values()),
            "Low Test":   list(self.calculate_metrics(test_low,    te_lp).values()),
        }
        print(f"\n{symb} METRICS for {timeframe}\n{pd.DataFrame(metrics)}")

        ukf.predict();      self._stabilise_P(ukf)
        high_ukf.predict(); self._stabilise_P(high_ukf)
        low_ukf.predict();  self._stabilise_P(low_ukf)
        return ukf, high_ukf, low_ukf

    @staticmethod
    def ukf_handler(data, ukf, high_ukf, low_ukf):
        for u, val in [(ukf, data["close"]), (high_ukf, data["high"]), (low_ukf, data["low"])]:
            u.update(val);  UKFModel._stabilise_P(u)
            u.predict();    UKFModel._stabilise_P(u)
        return ukf.x[0], high_ukf.x[0], low_ukf.x[0]


# ── Equity helper ─────────────────────────────────────────────────────────────
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


# ── Position helpers ──────────────────────────────────────────────────────────
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
            limit = 0.2 if symbol == "BTCUSDT" else 0.15
            upnl  = position_info["unrealized_pnl"]
            if upnl >= wallet_balance * limit or upnl >= 0.6:
                close_futures_position(position_info)
                logger.info(f"check_open_position: auto-closed {symbol} (uPNL={upnl:.4f})")
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


# ── SignalGenerator ───────────────────────────────────────────────────────────
# ── ATR (Average True Range) — live equivalent of walkforward v4's ATR SL ────

def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    Average True Range using Wilder's method, identical to the function of
    the same name in pyquant_walkforward_v4.py so live SL behaviour matches
    what was validated in the backtest.

    True Range = max(H-L, |H-prev_C|, |L-prev_C|)
    Returns 0.0 if fewer than 2 bars are available — sl_price() in
    SignalGenerator then falls back to the fixed-offset method, so a trade
    is never left without a stop loss due to an ATR data shortfall.
    """
    if len(df) < 2:
        return 0.0
    h = df["high"].values; l = df["low"].values; c = df["close"].values
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]),
                               np.abs(l[1:] - c[:-1])))
    n = min(period, len(tr))
    return float(np.mean(tr[-n:])) if n > 0 else 0.0


def fetch_recent_bars(symbol_or_symbols, hours: int = 20) -> pd.DataFrame | None:
    """
    Fetches the last `hours` of 1H bars for ATR computation.

    Uses an explicit `start` parameter (CryptoBarsRequest supports start/end/
    limit per Alpaca's documented API) rather than relying on the no-start
    default behaviour used elsewhere in this codebase — this guarantees a
    deterministic lookback window regardless of Alpaca's default range,
    which matters here because an under-filled ATR window silently produces
    a too-tight SL rather than an obvious error.

    hours=20 gives a comfortable buffer above the 14-period ATR requirement
    (needs 15 bars minimum) to absorb any single missing/thin bar.
    """
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
        try:
            with open("optimal_params.json", "r") as f:
                params = json.load(f)
            return params
        except FileNotFoundError:
            return {"strict_btc": 0.0025, "stricts_btc": 0.006,
                    "strict_eth": 0.004,  "stricts_eth": 0.008,
                    "atr_mult": 1.8}

    def _calculate_tp_price(self, price, pred, tp_multiplier, rounds, leverage, bal, tp_inc, entry, trend):
        net_bal = bal * tp_inc
        net_val = net_bal * leverage
        tp_qty  = (bal * leverage) / entry
        tp_candidate = net_val / tp_qty if tp_qty > 0 else pred
        if trend == "up":
            return round(tp_candidate, rounds) if pred > tp_candidate else round(pred, rounds)
        return round(tp_candidate, rounds) if pred < tp_candidate else round(pred, rounds)

    def _calculate_sl_price(self, entry_price, symbol, trend, atr=0.0, atr_mult=None):
        """
        ATR-adaptive SL, matching pyquant_walkforward_v4.py's sl_price() exactly.

        delta = max(atr_mult × ATR, fixed_floor)   when ATR data available
        delta = fixed_delta ($225 BTC / $16 ETH)    fallback otherwise

        The max() with a floor prevents the stop from compressing tighter
        than the asset's natural transaction-level noise during low-
        volatility periods (e.g. ATR briefly near zero on a quiet weekend).
        atr_mult and floors are read from optimal_params.json / hardcoded
        floors below — the same values validated in the walk-forward backtest
        that produced the +1060% / 91.4% win-rate result.
        """
        is_btc = (symbol == "BTC/USD")
        fixed_delta = (7.5 * 30) if is_btc else (16.0 * 1)   # $225 / $16
        floor_delta = 100.0 if is_btc else 8.0               # BTC_SL_FLOOR / ETH_SL_FLOOR
        rounds      = 0 if is_btc else 2

        if atr > 0 and atr_mult is not None:
            delta = max(atr_mult * atr, floor_delta)
        else:
            delta = fixed_delta

        if trend == "up":
            return round(entry_price - delta, rounds)
        return round(entry_price + delta, rounds)

    def _get_symbol_params(self, symbol: str, params: dict) -> dict:
        if symbol == "BTC/USD":
            tp_inc = 1.004
            return {
                "rr": 8.6, "trig_ratio": 6.1,
                "strict": params["strict_btc"], "trade_symbol": "BTCUSDT",
                "pct_diff_and_vol_strict": params["stricts_btc"],
                "buy_tp": 1.0075, "sell_tp": 2 - 1.0075,
                "first_loss": 7.5, "loss_multiple": 30, "rounds": 0,
                "tp_inc": tp_inc, "sell_inc": 2 - tp_inc,
            }
        elif symbol == "ETH/USD":
            tp_inc = 1.05
            return {
                "rr": 8.62, "trig_ratio": 6.02,
                "strict": params["strict_eth"], "trade_symbol": "ETHUSDT",
                "pct_diff_and_vol_strict": params["stricts_eth"],
                "buy_tp": 1.0085, "sell_tp": 2 - 1.0085,
                "first_loss": 16, "loss_multiple": 1, "rounds": 2,
                "tp_inc": tp_inc, "sell_inc": 2 - tp_inc,
            }
        raise ValueError(f"Unknown symbol: {symbol}")

    def _process_buy_signal(self, price, pred, high_pred, low_pred, symbol, bal,
                             symbol_params, params, lev, atr=0.0, atr_mult=None):
        buy_price    = low_pred
        tp_price_cand = pred
        prof = abs(1 - tp_price_cand / buy_price) if buy_price > 0 else 0
        r = symbol_params["rounds"]
        if not (tp_price_cand > buy_price and prof >= symbol_params["strict"]):
            self.sig_gened = False
            return None
        entry = round(price, r) if price < buy_price else round(buy_price, r)
        order_type = "Market" if price < buy_price else "Limit"
        self.sig_gened = True
        tp_price = self._calculate_tp_price(price, pred, symbol_params["buy_tp"], r, lev, bal, symbol_params["tp_inc"], entry, "up")
        sl_price = self._calculate_sl_price(entry, symbol, "up", atr, atr_mult)
        # Expected dollar loss if SL is hit, at THIS signal's own SL width.
        # Used by the live last-resort stop in pyq_p4.py as a threshold that
        # scales with whatever SL width was actually used (fixed or ATR),
        # instead of a fixed % of balance that could be tighter than a wide
        # ATR-based SL and fire before the real bracket order ever would.
        pos_est = (bal * lev) / entry if entry > 0 else 0
        expected_max_loss = abs(entry - sl_price) * pos_est
        return {"symbol": symbol_params["trade_symbol"], "order_type": order_type,
                "entry_price": entry, "order_side": "Buy",
                "tp_price": tp_price, "sl_price": sl_price,
                "current_price": price, "checked": False, "current_bal": bal,
                "atr": atr, "atr_mult": atr_mult, "expected_max_loss": expected_max_loss}

    def _process_sell_signal(self, price, pred, high_pred, low_pred, symbol, bal,
                              symbol_params, params, lev, atr=0.0, atr_mult=None):
        sell_price    = high_pred
        tp_price_cand = pred
        prof = abs(1 - sell_price / tp_price_cand) if tp_price_cand > 0 else 0
        r = symbol_params["rounds"]
        if not (sell_price > tp_price_cand and prof >= symbol_params["strict"]):
            self.sig_gened = False
            return None
        entry = round(price, r) if price > sell_price else round(sell_price, r)
        order_type = "Market" if price > sell_price else "Limit"
        self.sig_gened = True
        tp_price = self._calculate_tp_price(price, pred, symbol_params["sell_tp"], r, lev, bal, symbol_params["sell_inc"], entry, "down")
        sl_price = self._calculate_sl_price(entry, symbol, "down", atr, atr_mult)
        pos_est = (bal * lev) / entry if entry > 0 else 0
        expected_max_loss = abs(sl_price - entry) * pos_est
        return {"symbol": symbol_params["trade_symbol"], "order_type": order_type,
                "entry_price": entry, "order_side": "Sell",
                "tp_price": tp_price, "sl_price": sl_price,
                "current_price": price, "checked": False, "current_bal": bal,
                "atr": atr, "atr_mult": atr_mult, "expected_max_loss": expected_max_loss}

    def generate_signal(self, price, pred, high_pred, low_pred, symbol, leverage, bal,
                        atr=0.0, atr_mult=None):
        """
        atr/atr_mult: pass the live-computed ATR (see compute_atr/fetch_recent_bars
        above) and the atr_mult from optimal_params.json (written by the Bayesian
        grid_search.py) to use ATR-adaptive SL, matching pyquant_walkforward_v4.py.
        If atr=0.0 or atr_mult=None, falls back to the fixed $225/$16 SL offset —
        the system never generates a signal without a valid stop loss.
        """
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
        self.pct_diff = pct_diff
        params = self.load_params()
        try:
            sp = self._get_symbol_params(symbol, params)
        except ValueError:
            return None
        self.sig_vol = pct_diff <= sp["pct_diff_and_vol_strict"] or vol >= sp["pct_diff_and_vol_strict"]
        if not self.sig_vol:
            return None
        if not self.sig_gened:
            if trend == "up":
                sig = self._process_buy_signal(price, pred, high_pred, low_pred, symbol,
                                                bal, sp, params, leverage, atr, atr_mult)
            else:
                sig = self._process_sell_signal(price, pred, high_pred, low_pred, symbol,
                                                 bal, sp, params, leverage, atr, atr_mult)
            if sig:
                self.signals.append(sig)
                self.signal = sig
            return sig
        return None

    def get_state(self):
        return {"sig_gened": self.sig_gened, "sig_vol": self.sig_vol,
                "current_signal": self.signal, "trend": self.trend, "pct_diff": self.pct_diff}

    def reset(self):
        self.sig_gened = False; self.sig_vol = False
        self.signal = None; self.signals = []


# ── PositionGuard ─────────────────────────────────────────────────────────────
class PositionGuard:
    """
    Background service that monitors open orders and positions.
    Bracket orders now use POST /fapi/v1/algoOrder (Binance Algo Service).
    All conditional order types were migrated away from /fapi/v1/order on 2025-12-09.
    """

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

        # Signal store: persists signals by symbol so retries can find them
        # after the entry order is removed from monitored_orders.
        self._signal_store: Dict[str, Any] = {}

        # Tick guard: set at the START of _check_and_place_bracket (before any
        # API call), not after. Prevents orders-loop and positions-loop both
        # firing in the same scheduler tick regardless of success or failure.
        self._bracket_placed_this_tick: Set[str] = set()

        # Algo order pairs for slippage tracking and OCO cleanup.
        # Stores (sl_algo_id, tp_algo_id) per symbol after bracket placement.
        # Returns algoId not orderId — these are different ID spaces.
        self._bracket_orders: Dict[str, Dict[str, Any]] = {}

        self.slippage_log_file = "slippage_log.jsonl"
        self._api_lock = _binance_lock

        # Mode detection with retry — critical: a wrong default here causes
        # EVERY subsequent bracket order to fail with -4120.
        self._hedge_mode: bool = self._detect_hedge_mode()

    # ── Persistence ──────────────────────────────────────────────────────────
    def load_trade_state(self) -> dict:
        if os.path.exists(self.trade_state_file):
            try:
                with open(self.trade_state_file, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_to_json(self):
        try:
            with open(self.trade_state_file, "w") as f:
                json.dump(self.monitored_orders, f, indent=4, default=str)
        except Exception as e:
            logger.error(f"PositionGuard.save_to_json: {e}")

    def save_trade_state(self, symbol: str, side: str, entry, tp, sl):
        self.active_trades[symbol] = {"side": side, "entry": float(entry),
                                       "tp": float(tp), "sl": float(sl),
                                       "timestamp": datetime.now().isoformat()}
        with open(self.trade_state_file, "w") as f:
            json.dump(self.active_trades, f, indent=4)

    def clear_trade_state(self, symbol: str):
        if symbol in self.active_trades:
            del self.active_trades[symbol]
            with open(self.trade_state_file, "w") as f:
                json.dump(self.active_trades, f, indent=4)

    # ── Mode detection ────────────────────────────────────────────────────────
    def _detect_hedge_mode(self, max_retries: int = 5, retry_delay: int = 3) -> bool:
        """
        Confirms account position mode from Binance. Retries up to 5 times.

        One-way (dualSidePosition=False): positionSide=BOTH, reduceOnly=True
        Hedge   (dualSidePosition=True):  positionSide=LONG/SHORT, quantity required

        Never silently defaults — raises if all retries fail so the operator
        sees a clear startup error rather than -4120 on the first trade fill.
        """
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
                    logger.warning(f"[PositionGuard] Mode detection attempt {attempt}/{max_retries}: {e}. "
                                   f"Retrying in {retry_delay}s…")
                    time.sleep(retry_delay)
        raise ConnectionError(
            f"[PositionGuard] Cannot confirm position mode after {max_retries} attempts. "
            f"Last error: {last_err}. Check API key permissions."
        )

    # ── Slippage logging ──────────────────────────────────────────────────────
    def log_slippage(self, symbol: str, leg: str, side: str,
                      trigger_price: float, fill_price: float, order_id=None):
        try:
            if not trigger_price or not fill_price:
                return
            is_buy     = str(side).upper() == "BUY"
            signed_diff = (fill_price - trigger_price) if is_buy else (trigger_price - fill_price)
            slip_pct    = (signed_diff / trigger_price) * 100
            record = {
                "timestamp": datetime.now().isoformat(), "symbol": symbol,
                "leg": leg, "side": side, "order_id": order_id,
                "trigger_price": trigger_price, "fill_price": fill_price,
                "slippage_abs": signed_diff, "slippage_pct": slip_pct,
            }
            with open(self.slippage_log_file, "a") as f:
                f.write(json.dumps(record) + "\n")
            logger.info(f"📊 Slippage [{leg}] {symbol}: trigger={trigger_price} "
                        f"fill={fill_price} slip={slip_pct:+.4f}%")
        except Exception as e:
            logger.error(f"log_slippage ({symbol}, {leg}): {e}")

    # ── Telegram ──────────────────────────────────────────────────────────────
    async def send_telegram_alert(self, message: str):
        try:
            if self.telegram_app:
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
        await update.message.reply_text(
            f"🔄 PositionGuard\nOrders: {len(self.monitored_orders)}\n"
            f"Positions: {len(self.monitored_positions)}\nRunning: {self.running}")

    async def list_command(self, update: Update, context):
        msg = "📋 Monitored\n"
        for oid, info in self.monitored_orders.items():
            msg += f"  {info['symbol']} (ID:{oid})\n"
        await update.message.reply_text(msg or "Nothing monitored.")

    async def stop_command(self, update: Update, context):
        self.running = False
        await update.message.reply_text("🛑 Stopping…")

    # ── Main monitoring loop ──────────────────────────────────────────────────
    def check_all_orders_and_positions(self):
        if not self.monitored_orders and not self.monitored_positions and not self._bracket_orders:
            return

        # Reset per-tick guard at the START of each check cycle.
        self._bracket_placed_this_tick.clear()

        orders_to_remove = []
        for order_id, order_info in list(self.monitored_orders.items()):
            try:
                symbol = order_info["symbol"]
                with self._api_lock:
                    order_status = self.binance.futures_get_order(
                        symbol=symbol, orderId=int(order_id))
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
                                self.log_slippage(symbol, "ENTRY",
                                                   sig.get("order_side", "?"),
                                                   trigger, avg_price, order_id)
                        except Exception as slip_err:
                            logger.error(f"Entry slippage logging ({symbol}): {slip_err}")
                    orders_to_remove.append(order_id)
                    self._check_and_place_bracket(symbol, order_id=order_id, signal_override=sig)

                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    self.add_alert_to_queue(f"❌ Order {order_id} failed: {status}.")
                    orders_to_remove.append(order_id)
                    self._signal_store.pop(symbol, None)
                else:
                    logger.info(f"Order {order_id} status: {status}. Waiting…")

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
                        'get', 'algoOrders/current', signed=True,
                        data={"symbol": symbol})
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
                        self.add_alert_to_queue(f"🤔 No open position for {symbol}. Removing.")
                        self.monitored_positions.discard(symbol)
                        self._signal_store.pop(symbol, None)
                    else:
                        logger.info(f"{symbol} still needs TP/SL. Retrying…")
                        self._check_and_place_bracket(symbol,
                            signal_override=self._signal_store.get(symbol))
            except Exception as e:
                logger.error(f"Error checking position for {symbol}: {e}")

        # Check existing bracket pairs for fills and clean up dangling sibling orders
        self._check_bracket_fills()

    # ── Bracket order placement ───────────────────────────────────────────────
    def _check_and_place_bracket(self, symbol: str, order_id=None, signal_override=None):
        """Verifies a filled entry has TP/SL placed; places via Algo API if not."""

        # Set tick guard IMMEDIATELY — before any API call.
        # If set only on success (old behaviour), a failed attempt left the guard
        # unset so the positions loop fired a second attempt in the same tick.
        self._bracket_placed_this_tick.add(symbol)

        tp_sl_placed = False
        position_info = {"is_open_position": False, "symbol": symbol,
                          "side": None, "size": 0.0, "entry_price": None,
                          "unrealized_pnl": 0.0, "positionAmt": "0"}
        try:
            with self._api_lock:
                positions = self.binance.futures_position_information()
            raw_pos = next(
                (p for p in positions if p["symbol"] == symbol and float(p["positionAmt"]) != 0),
                None)
            if raw_pos:
                position_info.update({
                    "is_open_position": True,
                    "side":     "BUY" if float(raw_pos["positionAmt"]) > 0 else "SELL",
                    "size":     abs(float(raw_pos["positionAmt"])),
                    "entry_price": float(raw_pos["entryPrice"]),
                    "unrealized_pnl": float(raw_pos["unRealizedProfit"]),
                    "positionAmt": raw_pos["positionAmt"],
                })
            self.monitored_positions.add(symbol)

            # Resolve signal — prefer direct override, then monitored_orders, then _signal_store
            signal = signal_override
            if not signal and order_id and str(order_id) in self.monitored_orders:
                signal = self.monitored_orders[str(order_id)].get("signal")
            if not signal:
                for info in self.monitored_orders.values():
                    if info.get("symbol") == symbol and "signal" in info:
                        signal = info["signal"]; break
            if not signal:
                signal = self._signal_store.get(symbol)

            if not signal:
                self.add_alert_to_queue(f"⚠️ No signal for {symbol}. Using fallback TP/SL.")
                self._place_bracket_order_fallback(symbol, position_info)
                return

            # Confirm position is still open before placing bracket
            with self._api_lock:
                live_positions = self.binance.futures_position_information(symbol=symbol)
            live_pos = next((p for p in live_positions if float(p["positionAmt"]) != 0), None)
            if not live_pos:
                self.add_alert_to_queue(f"🤔 Entry filled but no open position for {symbol}.")
                self.monitored_positions.discard(symbol)
                self._signal_store.pop(symbol, None)
                return

            # Check for existing algo bracket
            with self._api_lock:
                open_algos = self.binance._request_futures_api(
                    'get', 'algoOrders/current', signed=True, data={"symbol": symbol})
            has_bracket = bool(open_algos.get("orders"))
            if has_bracket:
                self.add_alert_to_queue(f"🛡️ {symbol} already has algo TP/SL. Guard done.")
                self.monitored_positions.discard(symbol)
                self._signal_store.pop(symbol, None)
                tp_sl_placed = True
            else:
                resp  = self._place_bracket_order(symbol, live_pos, signal)
                sl_ok = resp.get("sl_order") is not None
                tp_ok = resp.get("tp_order") is not None
                if sl_ok or tp_ok:
                    sl_id = resp["sl_order"].get("algoId") if sl_ok else "N/A"
                    tp_id = resp["tp_order"].get("algoId") if tp_ok else "N/A"
                    self.add_alert_to_queue(f"🔒 Algo TP/SL placed for {symbol}. SL:{sl_id} TP:{tp_id}")
                    if sl_ok and tp_ok:
                        self.monitored_positions.discard(symbol)
                        self._signal_store.pop(symbol, None)
                    tp_sl_placed = True
                else:
                    logger.error(f"Both algo bracket legs failed for {symbol}. Will retry.")

            # Manual tracking fallback when bracket placement completely fails
            if not tp_sl_placed:
                try:
                    alpaca_sym = {"ETHUSDT": "ETH/USD", "BTCUSDT": "BTC/USD"}.get(symbol)
                    if alpaca_sym and signal:
                        position_status = check_open_position(symbol=symbol)
                        tr_data = None
                        for _ in range(3):
                            downloaded = data_download("1T", alpaca_sym)
                            if downloaded is not None:
                                tr_data = downloaded.iloc[-1]; break
                            time.sleep(2)
                        if tr_data is not None:
                            hhigh = tr_data["high"]; llow = tr_data["low"]
                            if signal["order_side"] == "Buy":
                                if hhigh >= signal["tp_price"] or llow <= signal["sl_price"]:
                                    close_futures_position(position_status)
                            elif signal["order_side"] == "Sell":
                                if llow <= signal["tp_price"] or hhigh >= signal["sl_price"]:
                                    close_futures_position(position_status)
                except Exception as e:
                    logger.error(f"Manual tracking fallback ({symbol}): {e}")

        except Exception as e:
            logger.error(f"_check_and_place_bracket fatal ({symbol}): {e}")
            self.add_alert_to_queue(f"🚨 CRITICAL: TP/SL management failed for {symbol}: {e}")

    def _place_bracket_order(self, symbol: str, position_info: dict, signal: dict) -> dict:
        """
        Places SL and TP via POST /fapi/v1/algoOrder with algoType=CONDITIONAL.

        This endpoint replaced /fapi/v1/order for all conditional order types
        on 2025-12-09. Using the old endpoint returns -4120 STOP_ORDER_SWITCH_ALGO.

        Parameters per Binance docs:
          algoType     = "CONDITIONAL"  (mandatory)
          type         = "STOP_MARKET" or "TAKE_PROFIT_MARKET"
          triggerPrice = the price level (not stopPrice — different field name)
          workingType  = "MARK_PRICE"  (harder to wick than CONTRACT_PRICE)
          positionSide = "BOTH" (One-way) or "LONG"/"SHORT" (Hedge)
          quantity     = position size in coins
          reduceOnly   = "true" (One-way only; incompatible with Hedge mode)

        Response returns algoId (not orderId) — stored in _bracket_orders for
        OCO cancellation when one leg fills.
        """
        sl_order = tp_order = None
        try:
            position_amt = float(position_info.get("positionAmt",
                                 position_info.get("size", 0)))
            is_long    = position_amt > 0
            close_side = "SELL" if is_long else "BUY"

            sl_price = float(signal["sl_price"])
            tp_price = float(signal["tp_price"])

            # Exchange precision
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

            # Direction guard — prevent Binance error -2021
            entry = float(position_info.get("entry_price") or position_info.get("entryPrice", 0))
            if entry > 0:
                if is_long:
                    if sl_price >= entry:
                        sl_price = round(round(entry * 0.995 / tick_size) * tick_size, price_prec)
                    if tp_price <= entry:
                        tp_price = round(round(entry * 1.005 / tick_size) * tick_size, price_prec)
                else:
                    if sl_price <= entry:
                        sl_price = round(round(entry * 1.005 / tick_size) * tick_size, price_prec)
                    if tp_price >= entry:
                        tp_price = round(round(entry * 0.995 / tick_size) * tick_size, price_prec)

            sl_str  = f"{sl_price:.{price_prec}f}"
            tp_str  = f"{tp_price:.{price_prec}f}"
            qty_str = f"{abs(position_amt):.{qty_prec}f}"
            pos_side = ("LONG" if is_long else "SHORT") if self._hedge_mode else "BOTH"

            logger.info(f"Placing algo bracket for {symbol} "
                        f"({'LONG' if is_long else 'SHORT'}, "
                        f"{'Hedge' if self._hedge_mode else 'One-way'}): "
                        f"entry={entry}  SL={sl_str}  TP={tp_str}")

            def _algo_params(order_type: str, trigger_price: str) -> dict:
                """Builds params for POST /fapi/v1/algoOrder."""
                params = {
                    "algoType":    "CONDITIONAL",
                    "symbol":      symbol,
                    "side":        close_side,
                    "type":        order_type,
                    "triggerPrice": trigger_price,   # NOTE: triggerPrice, not stopPrice
                    "quantity":    qty_str,
                    "workingType": "MARK_PRICE",
                    "positionSide": pos_side,
                }
                if not self._hedge_mode:
                    # reduceOnly only valid in One-way mode
                    params["reduceOnly"] = "true"
                return params

            with self._api_lock:
                sl_order = self.binance._request_futures_api(
                    'post', 'algoOrder', signed=True,
                    data=_algo_params("STOP_MARKET", sl_str))
                logger.info(f"SL algo order placed: {symbol} algoId={sl_order.get('algoId')} trigger={sl_str}")

                tp_order = self.binance._request_futures_api(
                    'post', 'algoOrder', signed=True,
                    data=_algo_params("TAKE_PROFIT_MARKET", tp_str))
                logger.info(f"TP algo order placed: {symbol} algoId={tp_order.get('algoId')} trigger={tp_str}")

        except Exception as e:
            logger.error(f"_place_bracket_order ({symbol}): {e}")

        # Register for OCO cleanup and slippage tracking
        if sl_order and tp_order:
            self._bracket_orders[symbol] = {
                "sl_algo_id": sl_order.get("algoId"),
                "tp_algo_id": tp_order.get("algoId"),
                "sl_trigger": float(sl_str) if sl_order else 0,
                "tp_trigger": float(tp_str) if tp_order else 0,
                "side":       "BUY" if is_long else "SELL",
            }

        return {"sl_order": sl_order, "tp_order": tp_order, "combined": True}

    def _place_bracket_order_fallback(self, symbol: str, position_info: dict, signal=None) -> tuple:
        """Fallback bracket using conservative 0.5% defaults."""
        try:
            position_amt = float(position_info.get("positionAmt",
                                 position_info.get("size", 0)))
            is_long    = position_amt > 0
            close_side = "SELL" if is_long else "BUY"
            entry_price = float(position_info.get("entry_price")
                                or position_info.get("entryPrice", 0))

            if signal:
                tp_price = float(signal["tp_price"])
                sl_price = float(signal["sl_price"])
            elif is_long:
                tp_price = entry_price * 1.005; sl_price = entry_price * 0.995
            else:
                tp_price = entry_price * 0.995; sl_price = entry_price * 1.005

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

            sl_price = round(round(sl_price / tick_size) * tick_size, price_prec)
            tp_price = round(round(tp_price / tick_size) * tick_size, price_prec)
            sl_str   = f"{sl_price:.{price_prec}f}"
            tp_str   = f"{tp_price:.{price_prec}f}"
            qty_str  = f"{abs(position_amt):.{qty_prec}f}"
            pos_side = ("LONG" if is_long else "SHORT") if self._hedge_mode else "BOTH"

            def _algo_params(order_type, trigger_price):
                p = {"algoType": "CONDITIONAL", "symbol": symbol, "side": close_side,
                     "type": order_type, "triggerPrice": trigger_price,
                     "quantity": qty_str, "workingType": "MARK_PRICE",
                     "positionSide": pos_side}
                if not self._hedge_mode:
                    p["reduceOnly"] = "true"
                return p

            with self._api_lock:
                fallback_sl = self.binance._request_futures_api(
                    'post', 'algoOrder', signed=True, data=_algo_params("STOP_MARKET", sl_str))
                fallback_tp = self.binance._request_futures_api(
                    'post', 'algoOrder', signed=True, data=_algo_params("TAKE_PROFIT_MARKET", tp_str))

            logger.info(f"Fallback algo bracket placed for {symbol}: SL={sl_str} TP={tp_str}")
            return fallback_tp, fallback_sl
        except Exception as e:
            logger.error(f"_place_bracket_order_fallback ({symbol}): {e}")
            return None, None

    # ── Bracket fill detection and OCO cleanup ────────────────────────────────
    def _check_bracket_fills(self):
        """
        Detects when one bracket leg fills (position closed), logs slippage,
        and cancels the dangling sibling algo order.

        Detection: checks position_information — if positionAmt == 0, one
        leg triggered. We then identify which by comparing the closing trade's
        fill price against the SL/TP trigger prices.

        OCO cleanup: Binance does NOT automatically link SL and TP orders.
        Without cancelling the sibling, it will trigger later and open an
        unintended new position in the opposite direction.
        """
        for symbol in list(self._bracket_orders.keys()):
            pair = self._bracket_orders[symbol]
            try:
                with self._api_lock:
                    positions = self.binance.futures_position_information(symbol=symbol)
                still_open = any(float(p["positionAmt"]) != 0 for p in positions)
                if still_open:
                    continue   # Position still open, bracket not triggered yet

                # Position closed — find which leg via recent trade history
                with self._api_lock:
                    trades = self.binance.futures_account_trades(symbol=symbol, limit=5)

                fill_price = None
                if trades:
                    closing_trades = [t for t in trades if t.get("realizedPnl", "0") != "0"]
                    if closing_trades:
                        fill_price = float(closing_trades[-1]["price"])
                    else:
                        fill_price = float(trades[-1]["price"])

                if fill_price:
                    side = pair["side"]  # BUY (was long) or SELL (was short)
                    sl_trig = pair["sl_trigger"]
                    tp_trig = pair["tp_trigger"]

                    if side == "BUY":
                        # Long closed: SL is below entry, TP is above entry
                        leg = "SL" if fill_price <= (sl_trig * 1.002) else "TP"
                        sibling_id = pair["tp_algo_id"] if leg == "SL" else pair["sl_algo_id"]
                        sibling_leg = "TP" if leg == "SL" else "SL"
                    else:
                        # Short closed: SL is above entry, TP is below entry
                        leg = "SL" if fill_price >= (sl_trig * 0.998) else "TP"
                        sibling_id = pair["tp_algo_id"] if leg == "SL" else pair["sl_algo_id"]
                        sibling_leg = "TP" if leg == "SL" else "SL"

                    trigger = sl_trig if leg == "SL" else tp_trig
                    self.log_slippage(symbol, leg, side, trigger, fill_price)
                    self._cancel_sibling_algo_order(sibling_id, symbol, sibling_leg)

                del self._bracket_orders[symbol]

            except Exception as e:
                logger.error(f"_check_bracket_fills ({symbol}): {e}")
                self._bracket_orders.pop(symbol, None)

    def _cancel_sibling_algo_order(self, algo_id, symbol: str, leg_name: str):
        """
        Cancels a dangling algo order using DELETE /fapi/v1/algoOrder.
        Uses algoId (not orderId — different ID space for algo orders).
        """
        if not algo_id:
            return
        try:
            with self._api_lock:
                self.binance._request_futures_api(
                    'delete', 'algoOrder', signed=True, data={"algoId": algo_id})
            logger.info(f"Cancelled dangling {leg_name} algo order {algo_id} for {symbol}.")
        except Exception as e:
            # Benign if already filled/cancelled between check and cancel
            logger.info(f"Could not cancel {leg_name} algo order {algo_id} ({symbol}): {e}")

    # ── Order registration ────────────────────────────────────────────────────
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

    # ── Telegram bot ──────────────────────────────────────────────────────────
    async def run_telegram_bot(self):
        _app_started = False
        try:
            self.telegram_app = Application.builder().token(self.telegram_token).build()
            self.telegram_app.add_handler(CommandHandler("status", self.status_command))
            self.telegram_app.add_handler(CommandHandler("list",   self.list_command))
            self.telegram_app.add_handler(CommandHandler("stop",   self.stop_command))
            await self.telegram_app.initialize()
            await self.telegram_app.start()
            _app_started = True
            await self.telegram_app.updater.start_polling()
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

    # ── Scheduler (PRIVATE instance) ─────────────────────────────────────────
    def run_scheduler(self):
        """
        Runs on a PRIVATE scheduler instance.

        Using the global `schedule` module (old behaviour) caused
        check_all_orders_and_positions to fire from two callers simultaneously:
          1. This background thread's own schedule.run_pending()
          2. The main orchestra loop's schedule.run_pending() on the same object
        Every action then appeared twice in the logs (double FILLED alerts,
        double bracket attempts, etc.).

        _schedule_module.Scheduler() creates a fully isolated instance invisible
        to the global schedule.run_pending() in pyquant_orchestra.py.
        """
        private_sched = _schedule_module.Scheduler()
        private_sched.every(30).seconds.do(self.check_all_orders_and_positions)
        self.add_alert_to_queue("🔔 PositionGuard scheduler ONLINE.")
        logger.info("PositionGuard scheduler started (private scheduler instance).")
        while self.running:
            private_sched.run_pending()
            time.sleep(1)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
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


# ── StateManager ──────────────────────────────────────────────────────────────
class StateManager:
    def __init__(self, state_file="bot_state.json", command_file="bot_commands.json"):
        self.state_file   = state_file
        self.command_file = command_file
        if not os.path.exists(self.command_file):
            with open(self.command_file, "w") as f:
                json.dump({"command": None}, f)

    def update_state(self, data: Dict[str, Any]):
        try:
            data["last_updated"] = datetime.now().isoformat()
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=4)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.error(f"StateManager.update_state: {e}")

    def get_command(self) -> str | None:
        try:
            if not os.path.exists(self.command_file):
                return None
            with open(self.command_file, "r") as f:
                data = json.load(f)
            cmd = data.get("command")
            if cmd:
                with open(self.command_file, "w") as f:
                    json.dump({"command": None}, f)
            return cmd
        except Exception as e:
            logger.error(f"StateManager.get_command: {e}")
            return None
