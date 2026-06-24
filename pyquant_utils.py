"""
pyquant_utils.py
Shared utilities: UKF model factory, data download, signal generation,
position management, and PositionGuard monitoring service.
All API credentials are loaded from the .env file.
"""

# ── Standard library ──────────────────────────────────────────────────────────
import os
import json
import math
import time
import logging
import hashlib
import hmac
import threading
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_FLOOR
from queue import Queue
from typing import Dict, Any, Set

# ── Third-party ───────────────────────────────────────────────────────────────
import numpy as np
import pandas as pd
import requests
import asyncio
import websocket
import matplotlib.pyplot as plt
import nest_asyncio

from dotenv import load_dotenv

import alpaca_trade_api as tradeapi
from alpaca_trade_api.stream import Stream
from alpaca_trade_api.common import URL
import datetime as dt

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

import schedule

from filterpy.kalman import UnscentedKalmanFilter, MerweScaledSigmaPoints
from filterpy.common import Q_discrete_white_noise
from sklearn.metrics import (
    mean_absolute_error, mean_squared_error,
    mean_absolute_percentage_error, r2_score
)

nest_asyncio.apply()

# ── Environment / credentials ─────────────────────────────────────────────────
load_dotenv()

ALPACA_API_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
BYBIT_API_KEY     = os.environ["BYBIT_API_KEY"]
BYBIT_SECRET_KEY  = os.environ["BYBIT_SECRET_KEY"]
BINANCE_API_KEY   = os.environ["BINANCE_API_KEY"]
BINANCE_SECRET_KEY = os.environ["BINANCE_SECRET_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

# ── Clients ───────────────────────────────────────────────────────────────────
api = tradeapi.REST(
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
    "https://data.alpaca.markets/v1beta3/crypto/us/bars"
)

start_date    = dt.date.today() - dt.timedelta(days=60)
end_date      = dt.date.today()

crypto_stream = CryptoDataStream(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)
crypto_client = CryptoHistoricalDataClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)
binance_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, testnet=False)

session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_SECRET_KEY)

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_fh = logging.FileHandler("pyq_p3.log")
_fh.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
_ch.setFormatter(_fmt)
_fh.setFormatter(_fmt)

if not logger.handlers:
    logger.addHandler(_ch)
    logger.addHandler(_fh)


# ── OHLCV aggregation ─────────────────────────────────────────────────────────

class OHLCVAggregator:
    @staticmethod
    def aggregate_ohlcv_data(df: pd.DataFrame, aggregation_minutes: int) -> pd.DataFrame | None:
        return aggregate_ohlcv_data(df, aggregation_minutes)


def aggregate_ohlcv_data(df: pd.DataFrame, aggregation_minutes: int) -> pd.DataFrame | None:
    """Re-samples OHLCV data to the specified number of minutes."""
    df_agg = df.copy()
    rules = {k: v for k, v in {
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"
    }.items() if k in df_agg.columns}

    if not rules:
        logger.error("aggregate_ohlcv_data: no standard OHLCV columns found.")
        return None

    freq = f"{aggregation_minutes}min"
    try:
        result = df_agg.resample(freq, level="timestamp").agg(rules)
        result.dropna(how="all", inplace=True)
        return result
    except Exception as e:
        logger.error(f"aggregate_ohlcv_data: resampling failed: {e}")
        return None


# ── Live data download ────────────────────────────────────────────────────────

def data_download(timeframe: str, or_symbols):
    """
    Downloads the most recent bars for `or_symbols` at `timeframe` granularity.
    Returns a DataFrame indexed by (timestamp, symbol).
    """
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
        mae  = mean_absolute_error(true_values, predictions)
        mse  = mean_squared_error(true_values, predictions)
        rmse = np.sqrt(mse)
        mape = mean_absolute_percentage_error(true_values, predictions)
        r2   = r2_score(true_values, predictions)
        return {"MAE": mae, "RMSE": rmse, "MAPE": mape, "R2": r2}

    def create_ukf_models(self, timeframe: str, or_symbols: list):
        """Downloads historical data, trains three UKFs (close/high/low) and returns them."""
        dt_step      = 1
        n_dim_state  = 2
        n_dim_meas   = 1
        frame        = timeframe

        # Select API timeframe
        tf_map = {"1H": TimeFrame.Hour, "15T": TimeFrame.Minute, "5T": TimeFrame.Minute}
        api_tf = tf_map.get(timeframe)
        if api_tf is None:
            logger.error(f"create_ukf_models: bad timeframe '{timeframe}'")
            return None, None, None

        if or_symbols == ["BTC/USD"]:
            symb = "BTC"
        elif or_symbols == ["ETH/USD"]:
            symb = "ETH"
        else:
            logger.error(f"create_ukf_models: unknown symbols {or_symbols}")
            return None, None, None

        request_params = CryptoBarsRequest(
            symbol_or_symbols=or_symbols,
            timeframe=api_tf,
            start=self.start_date
        )
        raw_df = self.crypto_client.get_crypto_bars(request_params).df

        if frame == "1H":
            data = raw_df
        elif frame == "15T":
            data = self.aggregator.aggregate_ohlcv_data(raw_df.copy(), 15)
            data.dropna(inplace=True)
        elif frame == "5T":
            data = self.aggregator.aggregate_ohlcv_data(raw_df.copy(), 5)
            data.dropna(inplace=True)
        else:
            logger.error(f"create_ukf_models: unhandled frame '{frame}'")
            return None, None, None

        def fx(x, dt): return np.array([x[0] + dt * x[1], x[1]])
        def hx(x):     return np.array([x[0]])

        close_prices = data["close"].values
        high_prices  = data["high"].values
        low_prices   = data["low"].values

        train_size = int(len(close_prices) * 0.7)
        train_close = close_prices[:train_size]; test_close = close_prices[train_size:]
        train_high  = high_prices[:train_size];  test_high  = high_prices[train_size:]
        train_low   = low_prices[:train_size];   test_low   = low_prices[train_size:]

        best_params = (
            {"alpha": 0.001, "beta": 4.0, "kappa": 1, "P": 0.1,   "Q": 1.0, "R": 0.01}
            if symb == "ETH" else
            {"alpha": 0.001, "beta": 7.0, "kappa": 0, "P": 0.001, "Q": 1.0, "R": 0.01}
        )

        alpha, beta, kappa = best_params["alpha"], best_params["beta"], best_params["kappa"]
        P, Q, R = best_params["P"], best_params["Q"], best_params["R"]
        points  = MerweScaledSigmaPoints(n=n_dim_state, alpha=alpha, beta=beta, kappa=kappa)

        def _make_ukf(init_val):
            u = UnscentedKalmanFilter(
                dim_x=n_dim_state, dim_z=n_dim_meas,
                fx=fx, hx=hx, dt=dt_step, points=points
            )
            u.P = np.eye(n_dim_state) * P
            u.Q = Q_discrete_white_noise(dim=n_dim_state, dt=dt_step, var=0.004) * Q
            u.R = np.eye(n_dim_meas) * R
            u.x = np.array([init_val, 0])
            return u

        ukf      = _make_ukf(train_close[0])
        high_ukf = _make_ukf(train_high[0])
        low_ukf  = _make_ukf(train_low[0])

        # Train
        train_preds, test_preds = [], []
        for z in train_close: ukf.predict(); train_preds.append(ukf.x[0]); ukf.update(z)
        for z in test_close:  ukf.predict(); test_preds.append(ukf.x[0]);  ukf.update(z)

        htr_preds, hte_preds = [], []
        for z in train_high: high_ukf.predict(); htr_preds.append(high_ukf.x[0]); high_ukf.update(z)
        for z in test_high:  high_ukf.predict(); hte_preds.append(high_ukf.x[0]);  high_ukf.update(z)

        ltr_preds, lte_preds = [], []
        for z in train_low: low_ukf.predict(); ltr_preds.append(low_ukf.x[0]); low_ukf.update(z)
        for z in test_low:  low_ukf.predict(); lte_preds.append(low_ukf.x[0]);  low_ukf.update(z)

        ukf.predict(); high_ukf.predict(); low_ukf.predict()

        # Metrics
        metric_data = {
            "Metric": ["MAE", "RMSE", "MAPE", "R2"],
            "Train": list(self.calculate_metrics(train_close, train_preds).values()),
            "Test":  list(self.calculate_metrics(test_close,  test_preds).values()),
            "High Train": list(self.calculate_metrics(train_high, htr_preds).values()),
            "High Test":  list(self.calculate_metrics(test_high,  hte_preds).values()),
            "Low Train":  list(self.calculate_metrics(train_low,  ltr_preds).values()),
            "Low Test":   list(self.calculate_metrics(test_low,   lte_preds).values()),
        }
        print(f"{symb} METRICS for {frame}\n{pd.DataFrame(metric_data)}")

        return ukf, high_ukf, low_ukf

    @staticmethod
    def ukf_handler(data, ukf, high_ukf, low_ukf):
        """Updates all three UKFs with the latest bar and returns next-step predictions."""
        high  = data["high"]
        low   = data["low"]
        price = data["close"]

        ukf.update(price);      ukf.predict();      pred      = ukf.x[0]
        high_ukf.update(high);  high_ukf.predict(); high_pred = high_ukf.x[0]
        low_ukf.update(low);    low_ukf.predict();  low_pred  = low_ukf.x[0]

        return pred, high_pred, low_pred


# ── Equity helper ─────────────────────────────────────────────────────────────

def get_equities() -> tuple:
    """Returns (usdt_equity, btc_equity, eth_equity) from Binance Futures account."""
    usdt_equity = btc_equity = eth_equity = 0.0
    try:
        account_info = binance_client.futures_account()
        for asset in account_info["assets"]:
            name = asset["asset"]
            if name == "USDT":
                usdt_equity = float(asset["walletBalance"])
            elif name == "BTC":
                btc_equity  = float(asset["walletBalance"])
            elif name == "ETH":
                eth_equity  = float(asset["walletBalance"])
    except Exception as e:
        logger.error(f"get_equities: {e}")
    return usdt_equity, btc_equity, eth_equity


# ── Position helpers (also used by PositionGuard) ────────────────────────────

def check_open_position(symbol: str) -> dict:
    """Returns a dict describing any open Binance Futures position for `symbol`."""
    position_info = {
        "is_open_position": False, "symbol": symbol,
        "side": None, "size": 0.0, "entry_price": None,
        "unrealized_pnl": 0.0, "liquidation_price": None,
        "take_profit_price": None, "stop_loss_price": None,
    }
    try:
        positions     = binance_client.futures_position_information()
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
                logger.info(
                    f"check_open_position: auto-closed {symbol} due to profit "
                    f"(uPNL={upnl:.4f})"
                )
    except Exception as e:
        logger.error(f"check_open_position ({symbol}): {e}")
    return position_info


def close_futures_position(position_data: dict) -> dict:
    """Closes an open Binance Futures position with a market reduceOnly order."""
    try:
        symbol     = position_data["symbol"]
        side       = position_data["side"]
        size       = position_data["size"]
        close_side = "SELL" if side == "BUY" else "BUY"
        response   = binance_client.futures_create_order(
            symbol=symbol, side=close_side,
            type="MARKET", quantity=size, reduceOnly=True
        )
        logger.info(f"close_futures_position: closed {symbol} via {close_side} market order.")
        return response
    except Exception as e:
        err = f"close_futures_position ({position_data.get('symbol')}): {e}"
        logger.error(err)
        return {"error": err}


# ── SignalGenerator ───────────────────────────────────────────────────────────

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
            print(f"Loaded optimal params: {params}")
            return params
        except FileNotFoundError:
            print("optimal_params.json not found – using defaults.")
            return {
                "strict_btc":  0.0025, "stricts_btc": 0.006,
                "strict_eth":  0.004,  "stricts_eth": 0.008,
            }

    def _calculate_tp_price(self, price, pred, tp_multiplier, rounds, leverage, bal, tp_inc, entry, trend):
        net_bal = bal * tp_inc
        net_val = net_bal * leverage
        tp_qty  = (bal * leverage) / entry

        tp_candidate = net_val / tp_qty
        if trend == "up":
            return round(tp_candidate, rounds) if pred > tp_candidate else round(pred, rounds)
        else:
            return round(tp_candidate, rounds) if pred < tp_candidate else round(pred, rounds)

    def _calculate_sl_price(self, entry_price, positions, max_loss, rr, symbol, trend, tp_price=None):
        if trend == "up":
            if symbol in ("BTC/USD", "ETH/USD"):
                return entry_price - (max_loss / (entry_price * positions))
            return entry_price - (1 / rr * (tp_price - entry_price))
        else:
            if symbol in ("BTC/USD", "ETH/USD"):
                return entry_price + (max_loss / (entry_price * positions))
            return entry_price + (1 / rr * (entry_price - tp_price))

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

    def _process_signal(self, price, pred, high_pred, low_pred, symbol, bal, trend, symbol_params, lev):
        params = self.load_params()
        self.sig_vol  = False
        self.pct_diff = None

        pct_diff = abs(1 - pred / price) if trend == "up" else abs(1 - price / pred)
        self.pct_diff = pct_diff
        vol = 1 - (high_pred / low_pred)

        if pct_diff <= symbol_params["pct_diff_and_vol_strict"] or vol >= symbol_params["pct_diff_and_vol_strict"]:
            print(f"Primary siggen criteria met for {symbol}. Checking secondary…")
            self.sig_vol = True
        else:
            print("Primary siggen criteria NOT met.")
            return None

        max_loss = float(bal) * symbol_params["first_loss"] * symbol_params["loss_multiple"]

        if trend == "up":
            return self._process_buy_signal(
                price, pred, high_pred, low_pred, symbol, bal,
                symbol_params, max_loss, params, lev
            )
        return self._process_sell_signal(
            price, pred, high_pred, low_pred, symbol, bal,
            symbol_params, max_loss, params, lev
        )

    def _process_buy_signal(self, price, pred, high_pred, low_pred, symbol, bal,
                             symbol_params, max_loss, params, lev):
        buy_price    = low_pred
        tp_price_cand = pred
        prof = abs(1 - tp_price_cand / buy_price)

        if tp_price_cand > buy_price and prof >= symbol_params["strict"]:
            print("BUY signal passed secondary check.")
            if price < buy_price:
                entry_price = round(price, symbol_params["rounds"])
                order_type  = "Market"
            else:
                entry_price = round(buy_price, symbol_params["rounds"])
                order_type  = "Limit"
            self.sig_gened = True

            tp_price = self._calculate_tp_price(
                price, pred, symbol_params["buy_tp"], symbol_params["rounds"],
                lev, bal, symbol_params["tp_inc"], entry_price, "up"
            )
            trigger_price = round(
                entry_price - (1 / symbol_params["trig_ratio"] * (tp_price - entry_price)),
                symbol_params["rounds"]
            )
            positions = bal / entry_price
            sl_price  = round(
                self._calculate_sl_price(entry_price, positions, max_loss,
                                         symbol_params["rr"], symbol, "up", tp_price),
                symbol_params["rounds"]
            )
            return {
                "symbol": symbol_params["trade_symbol"], "order_type": order_type,
                "entry_price": entry_price, "order_side": "Buy",
                "tp_price": tp_price, "sl_price": sl_price,
                "trigger_price": trigger_price, "orderLinkId": None,
                "current_price": price, "checked": False, "current_bal": bal,
            }
        print("Secondary check failed – no BUY signal.")
        self.sig_gened = False
        return None

    def _process_sell_signal(self, price, pred, high_pred, low_pred, symbol, bal,
                              symbol_params, max_loss, params, lev):
        sell_price    = high_pred
        tp_price_cand = pred
        prof = abs(1 - sell_price / tp_price_cand)

        if sell_price > tp_price_cand and prof >= symbol_params["strict"]:
            print("SELL signal passed secondary check.")
            if price > sell_price:
                entry_price = round(price, symbol_params["rounds"])
                order_type  = "Market"
            else:
                entry_price = round(sell_price, symbol_params["rounds"])
                order_type  = "Limit"
            self.sig_gened = True

            tp_price = self._calculate_tp_price(
                price, pred, symbol_params["sell_tp"], symbol_params["rounds"],
                lev, bal, symbol_params["sell_inc"], entry_price, "down"
            )
            trigger_price = round(
                entry_price + (1 / symbol_params["trig_ratio"] * (entry_price - tp_price)),
                symbol_params["rounds"]
            )
            positions = bal / entry_price
            sl_price  = round(
                self._calculate_sl_price(entry_price, positions, max_loss,
                                         symbol_params["rr"], symbol, "down", tp_price),
                symbol_params["rounds"]
            )
            return {
                "symbol": symbol_params["trade_symbol"], "order_type": order_type,
                "entry_price": entry_price, "order_side": "Sell",
                "tp_price": tp_price, "sl_price": sl_price,
                "trigger_price": trigger_price, "orderLinkId": None,
                "current_price": price, "checked": False, "current_bal": bal,
            }
        print("Secondary check failed – no SELL signal.")
        self.sig_gened = False
        return None

    def generate_signal(self, price, pred, high_pred, low_pred, symbol, leverage, bal):
        """Entry point: returns a signal dict or None."""
        print("Starting Siggen Services")
        self.signal = None
        self.trend  = None

        if pred > price:
            trend = self.trend = "up"
        elif price > pred:
            trend = self.trend = "down"
        else:
            print(f"Flat price – no signal (price={price}, pred={pred})")
            return None

        params = self.load_params()
        try:
            symbol_params = self._get_symbol_params(symbol, params)
        except ValueError as e:
            print(e); return None

        try:
            if not self.sig_gened:
                signal = self._process_signal(
                    price, pred, high_pred, low_pred, symbol, bal,
                    trend, symbol_params, leverage
                )
                if signal:
                    print("Signal generated.")
                    self.signals.append(signal)
                    self.signal = signal
                    return signal
                self.sig_gened = False
                return None
        except Exception as e:
            print(f"Siggen error: {e}")
            print(traceback.format_exc())
        return None

    def get_state(self) -> dict:
        return {
            "sig_gened": self.sig_gened, "sig_vol": self.sig_vol,
            "stake": self.stake, "current_signal": self.signal,
            "all_signals": self.signals.copy(), "trend": self.trend,
            "pct_diff": self.pct_diff,
        }

    def reset(self):
        self.sig_gened = False
        self.sig_vol   = False
        self.signal    = None
        self.signals   = []
        print("SignalGenerator state reset.")


# ── PositionGuard ─────────────────────────────────────────────────────────────

class PositionGuard:
    """
    Background service that monitors open orders and positions.
    - Checks order fill status every 30 seconds.
    - Places/verifies TP/SL via Binance Futures stop orders.
    - Falls back to 1-minute manual tracking if bracket placement fails.
    - Persists state to active_trades.json so restarts are safe.
    - Sends Telegram alerts for every significant event.
    """

    def __init__(self, binance_client: Client, telegram_token: str, chat_id: str):
        self.binance           = binance_client
        self.telegram_token    = telegram_token
        self.telegram_chat_id  = chat_id
        self.telegram_app      = None

        self.trade_state_file  = "active_trades.json"
        self.active_trades     = self.load_trade_state()
        self.monitored_orders: Dict[str, Dict[str, Any]] = self.load_trade_state()
        self.monitored_positions: Set[str] = set()
        self.alert_queue       = Queue()

        # FIX: `running` must be initialised here so async methods can reference it
        self.running           = False
        self.scheduler_thread  = None
        self.telegram_thread   = None

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
        """FIX: Saves monitored_orders to disk so the 1-minute task can read it."""
        try:
            with open(self.trade_state_file, "w") as f:
                json.dump(self.monitored_orders, f, indent=4, default=str)
        except Exception as e:
            logger.error(f"PositionGuard.save_to_json: {e}")

    def save_trade_state(self, symbol: str, side: str, entry, tp, sl):
        self.active_trades[symbol] = {
            "side": side, "entry": float(entry),
            "tp": float(tp), "sl": float(sl),
            "timestamp": datetime.now().isoformat(),
        }
        with open(self.trade_state_file, "w") as f:
            json.dump(self.active_trades, f, indent=4)

    def clear_trade_state(self, symbol: str):
        if symbol in self.active_trades:
            del self.active_trades[symbol]
            with open(self.trade_state_file, "w") as f:
                json.dump(self.active_trades, f, indent=4)

    # ── Telegram ──────────────────────────────────────────────────────────────

    async def send_telegram_alert(self, message: str):
        try:
            if self.telegram_app:
                await self.telegram_app.bot.send_message(
                    chat_id=self.telegram_chat_id, text=message
                )
            logger.info(f"Telegram: {message}")
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

    # ── Telegram bot commands ─────────────────────────────────────────────────

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = (
            f"🔄 PositionGuard Status\n"
            f"Monitored Orders:    {len(self.monitored_orders)}\n"
            f"Monitored Positions: {len(self.monitored_positions)}\n"
            f"Alert Queue:         {self.alert_queue.qsize()}\n"
            f"Running:             {self.running}"
        )
        await update.message.reply_text(msg)

    async def list_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.monitored_orders and not self.monitored_positions:
            await update.message.reply_text("No active orders or positions being monitored.")
            return
        msg = "📋 Monitored Items\n"
        if self.monitored_orders:
            msg += "\n📝 Orders:\n"
            for oid, info in self.monitored_orders.items():
                msg += f"  {info['symbol']} (ID:{oid}, {info.get('status','?')})\n"
        if self.monitored_positions:
            msg += "\n🔒 Positions:\n"
            for sym in self.monitored_positions:
                msg += f"  {sym}\n"
        await update.message.reply_text(msg)

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.running = False
        await update.message.reply_text("🛑 PositionGuard stopping…")

    # ── Core monitoring loop ──────────────────────────────────────────────────

    def check_all_orders_and_positions(self):
        if not self.monitored_orders and not self.monitored_positions:
            return

        orders_to_remove = []
        for order_id, order_info in list(self.monitored_orders.items()):
            try:
                symbol       = order_info["symbol"]
                order_status = self.binance.futures_get_order(symbol=symbol, orderId=order_id)
                status       = order_status.get("status")
                order_info["status"] = status

                if status == "FILLED":
                    self.add_alert_to_queue(f"✅ Order {order_id} FILLED for {symbol}.")
                    self.save_to_json()
                    orders_to_remove.append(order_id)
                    self._check_and_place_bracket(symbol, order_id=order_id)

                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    self.add_alert_to_queue(f"❌ Order {order_id} failed: {status}.")
                    orders_to_remove.append(order_id)

                else:
                    logger.info(f"Order {order_id} status: {status}. Waiting…")

            except Exception as e:
                logger.error(f"Error checking order {order_id}: {e}")

        for oid in orders_to_remove:
            self.monitored_orders.pop(oid, None)
        if orders_to_remove:
            self.save_to_json()

        for symbol in list(self.monitored_positions):
            try:
                open_orders = self.binance.futures_get_open_orders(symbol=symbol)
                has_bracket = any(
                    o for o in open_orders
                    if o["type"] in ("TAKE_PROFIT", "STOP_MARKET", "TAKE_PROFIT_MARKET")
                )
                if has_bracket:
                    self.add_alert_to_queue(
                        f"🛡️ {symbol} has active TP/SL. Removing from monitoring."
                    )
                    self.monitored_positions.discard(symbol)
                else:
                    positions = self.binance.futures_position_information(symbol=symbol)
                    position  = next(
                        (p for p in positions if float(p["positionAmt"]) != 0), None
                    )
                    if not position:
                        self.add_alert_to_queue(f"🤔 No open position for {symbol}. Removing.")
                        self.monitored_positions.discard(symbol)
                    else:
                        logger.info(f"{symbol} still needs TP/SL. Retrying…")
                        self._check_and_place_bracket(symbol)
            except Exception as e:
                logger.error(f"Error checking position for {symbol}: {e}")
                self.add_alert_to_queue(f"⚠️ Error checking position for {symbol}: {e}")

    # ── Bracket order placement ───────────────────────────────────────────────

    def _check_and_place_bracket(self, symbol: str, order_id=None):
        """Verifies a filled order has TP/SL; places them if not."""
        tp_sl_placed = False
        # FIX: initialise position_info in scope so the fallback block can reference it
        position_info = {
            "is_open_position": False, "symbol": symbol,
            "side": None, "size": 0.0, "entry_price": None,
            "unrealized_pnl": 0.0, "liquidation_price": None,
            "positionAmt": "0",
        }

        try:
            # Find the live position
            positions = self.binance.futures_position_information()
            raw_pos   = next(
                (p for p in positions if p["symbol"] == symbol and float(p["positionAmt"]) != 0),
                None
            )
            if raw_pos:
                position_info.update({
                    "is_open_position": True,
                    "side":             "BUY" if float(raw_pos["positionAmt"]) > 0 else "SELL",
                    "size":             abs(float(raw_pos["positionAmt"])),
                    "entry_price":      float(raw_pos["entryPrice"]),
                    "unrealized_pnl":   float(raw_pos["unRealizedProfit"]),
                    "liquidation_price": float(raw_pos["liquidationPrice"]),
                    "positionAmt":      raw_pos["positionAmt"],
                })

            self.monitored_positions.add(symbol)

            # Retrieve the matching signal
            signal = None
            if order_id and str(order_id) in self.monitored_orders:
                signal = self.monitored_orders[str(order_id)].get("signal")
            if not signal:
                for info in self.monitored_orders.values():
                    if info.get("symbol") == symbol and "signal" in info:
                        signal = info["signal"]
                        break

            if not signal:
                self.add_alert_to_queue(f"⚠️ No signal found for {symbol}. Using fallback TP/SL.")
                self._place_bracket_order_fallback(symbol, position_info)
                return

            # Check if position still open
            live_positions = self.binance.futures_position_information(symbol=symbol)
            live_pos       = next(
                (p for p in live_positions if float(p["positionAmt"]) != 0), None
            )
            if not live_pos:
                self.add_alert_to_queue(f"🤔 Entry filled but no open position for {symbol}.")
                self.monitored_positions.discard(symbol)
                return

            # Check existing TP/SL
            open_orders = self.binance.futures_get_open_orders(symbol=symbol)
            has_bracket = any(
                o for o in open_orders
                if o["type"] in ("TAKE_PROFIT", "STOP_MARKET", "TAKE_PROFIT_MARKET")
            )
            if has_bracket:
                self.add_alert_to_queue(f"🛡️ {symbol} already has TP/SL. Guard done.")
                self.monitored_positions.discard(symbol)
                tp_sl_placed = True
            else:
                try:
                    resp = self._place_bracket_order(symbol, live_pos, signal)
                    sl_id = resp["sl_order"]["orderId"] if resp.get("sl_order") else "N/A"
                    tp_id = resp["tp_order"]["orderId"] if resp.get("tp_order") else "N/A"
                    self.add_alert_to_queue(
                        f"🔒 TP/SL placed for {symbol}. SL:{sl_id}  TP:{tp_id}"
                    )
                    tp_sl_placed = True
                except Exception as e:
                    logger.error(f"_place_bracket_order failed for {symbol}: {e}")
                    try:
                        self._place_bracket_order_fallback(symbol, live_pos, signal)
                        tp_sl_placed = True
                    except Exception as e2:
                        logger.error(f"Fallback also failed for {symbol}: {e2}")

            # Manual TP/SL tracking safety net
            if not tp_sl_placed:
                try:
                    # FIX: use assignment, not comparison
                    if symbol == "ETHUSDT":
                        symbolis = "ETH/USD"
                    elif symbol == "BTCUSDT":
                        symbolis = "BTC/USD"
                    else:
                        symbolis = None

                    position_status = check_open_position(symbol=symbol)
                    tr_data = None
                    for _ in range(3):
                        downloaded = data_download("1T", symbolis)
                        if downloaded is not None:
                            tr_data = downloaded.iloc[-1]
                            break
                        time.sleep(2)

                    if tr_data is not None and signal:
                        price = tr_data["close"]
                        llow  = tr_data["low"]
                        hhigh = tr_data["high"]
                        logger.info(f"Manual tracking {symbol} {signal['order_side']}")
                        if signal["order_side"] == "Buy":
                            if hhigh >= signal["tp_price"] or llow <= signal["sl_price"]:
                                close_futures_position(position_status)
                        elif signal["order_side"] == "Sell":
                            if llow <= signal["tp_price"] or hhigh >= signal["sl_price"]:
                                close_futures_position(position_status)
                except Exception as e:
                    logger.error(f"Manual tracking fallback failed for {symbol}: {e}")

        except Exception as e:
            logger.error(f"_check_and_place_bracket fatal error for {symbol}: {e}")
            self.add_alert_to_queue(f"🚨 CRITICAL: TP/SL management failed for {symbol}: {e}")

    def _place_bracket_order(self, symbol: str, position_info: dict, signal: dict) -> dict:
        """Places STOP_MARKET and TAKE_PROFIT_MARKET orders using signal TP/SL values."""
        sl_order = tp_order = None
        try:
            position_amt = float(position_info["positionAmt"])
            side         = "SELL" if position_amt > 0 else "BUY"

            tp_price = float(signal["tp_price"])
            sl_price = float(signal["sl_price"])

            # Get tick size
            info        = self.binance.futures_exchange_info()
            symbol_info = next(s for s in info["symbols"] if s["symbol"] == symbol)
            pf          = next(f for f in symbol_info["filters"] if f["filterType"] == "PRICE_FILTER")
            tick_size   = float(pf["tickSize"])
            ts_str      = str(tick_size)
            price_prec  = len(ts_str.split(".")[1].rstrip("0")) if "." in ts_str else 0

            sl_price = round(sl_price / tick_size) * tick_size
            tp_price = round(tp_price / tick_size) * tick_size
            sl_str   = f"{sl_price:.{price_prec}f}"
            tp_str   = f"{tp_price:.{price_prec}f}"

            # FIX: use self.binance, use stopPrice (not triggerPrice), use correct prices
            sl_order = self.binance.futures_create_order(
                symbol=symbol, side=side, type="STOP_MARKET",
                stopPrice=sl_str, closePosition=True, timeInForce="GTC"
            )
            logger.info(f"SL order placed for {symbol}: {sl_order.get('orderId')}")

            # FIX: was incorrectly using sl_price_str for TP trigger
            tp_order = self.binance.futures_create_order(
                symbol=symbol, side=side, type="TAKE_PROFIT_MARKET",
                stopPrice=tp_str, closePosition=True, timeInForce="GTC"
            )
            logger.info(f"TP order placed for {symbol}: {tp_order.get('orderId')}")

        except Exception as e:
            logger.error(f"_place_bracket_order ({symbol}): {e}")

        return {"sl_order": sl_order, "tp_order": tp_order, "combined": True}

    def _place_bracket_order_fallback(self, symbol: str, position_info: dict, signal=None) -> tuple:
        """Fallback bracket: calculates default TP/SL if no signal is available."""
        try:
            position_amt = float(position_info.get("positionAmt", 0))
            side         = "SELL" if position_amt > 0 else "BUY"
            entry_price  = float(position_info.get("entry_price") or position_info.get("entryPrice", 0))

            if signal:
                tp_price = float(signal["tp_price"])
                sl_price = float(signal["sl_price"])
            elif side == "SELL":
                tp_price = entry_price * 1.02
                sl_price = entry_price * 0.98
            else:
                tp_price = entry_price * 0.98
                sl_price = entry_price * 1.02

            info        = self.binance.futures_exchange_info()
            symbol_info = next(s for s in info["symbols"] if s["symbol"] == symbol)
            pf          = next(f for f in symbol_info["filters"] if f["filterType"] == "PRICE_FILTER")
            tick_size   = float(pf["tickSize"])
            tp_price    = round(tp_price / tick_size) * tick_size
            sl_price    = round(sl_price / tick_size) * tick_size

            # FIX: use futures_create_order with standard params (no algoType)
            fallback_sl = self.binance.futures_create_order(
                symbol=symbol, side=side, type="STOP_MARKET",
                stopPrice=sl_price, closePosition=True, timeInForce="GTC"
            )
            fallback_tp = self.binance.futures_create_order(
                symbol=symbol, side=side, type="TAKE_PROFIT_MARKET",
                stopPrice=tp_price, closePosition=True, timeInForce="GTC"
            )
            return fallback_tp, fallback_sl

        except Exception as e:
            logger.error(f"_place_bracket_order_fallback ({symbol}): {e}")
            return None, None

    # ── Order registration ────────────────────────────────────────────────────

    def start_guard_for_order(self, signal: Dict[str, Any], order_id: int):
        """Registers a new order for monitoring and persists to JSON."""
        symbol = signal["symbol"]
        # FIX: store as string key so JSON round-trips cleanly
        self.monitored_orders[str(order_id)] = {
            "symbol":     symbol,
            "signal":     signal,
            "status":     "NEW",
            "added_time": datetime.now().isoformat(),
        }
        self.save_to_json()
        msg = f"👮 PositionGuard monitoring order {order_id} for {symbol}."
        self.add_alert_to_queue(msg)
        logger.info(msg)

    # ── Telegram bot runner ───────────────────────────────────────────────────

    async def run_telegram_bot(self):
        try:
            self.telegram_app = Application.builder().token(self.telegram_token).build()
            self.telegram_app.add_handler(CommandHandler("status", self.status_command))
            self.telegram_app.add_handler(CommandHandler("list",   self.list_command))
            self.telegram_app.add_handler(CommandHandler("stop",   self.stop_command))
            await self.telegram_app.initialize()
            await self.telegram_app.start()
            await self.telegram_app.updater.start_polling()
            await self.process_alert_queue()
        except Exception as e:
            logger.error(f"Telegram bot error: {e}")
        finally:
            if self.telegram_app:
                await self.telegram_app.stop()

    # ── Scheduler runner ──────────────────────────────────────────────────────

    def run_scheduler(self):
        schedule.every(30).seconds.do(self.check_all_orders_and_positions)
        self.add_alert_to_queue("🔔 PositionGuard scheduler ONLINE.")
        logger.info("PositionGuard scheduler started.")
        while self.running:                # FIX: respect self.running
            schedule.run_pending()
            time.sleep(1)                  # FIX: single sleep (was doubled)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self.running = True

        self.scheduler_thread = threading.Thread(
            target=self.run_scheduler, daemon=True
        )
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
    """Manages bot_state.json / bot_commands.json for external dashboard integration."""

    def __init__(self, state_file="bot_state.json", command_file="bot_commands.json"):
        self.state_file   = state_file
        self.command_file = command_file
        self._ensure_files_exist()

    def _ensure_files_exist(self):
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
                logger.info(f"StateManager received command: {cmd}")
                with open(self.command_file, "w") as f:
                    json.dump({"command": None}, f)
            return cmd
        except Exception as e:
            logger.error(f"StateManager.get_command: {e}")
            return None
