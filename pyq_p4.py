"""
pyq_p4.py
PyQuant live trading module — major refactor integrating the validated
pyquant_walkforward_v4.py backtest design into live production.

CHANGES FROM pyq_p3.py
─────────────────────
1. LEVERAGE reduced from 50x to 10x, matching the walk-forward backtest
   that produced +1060% / 91.4% win rate / Sharpe 15.26 / max drawdown 28.28%.
   Every risk figure in this file assumes 10x; do not raise it without
   re-validating in the backtest first — the SL widths and last-resort
   thresholds below are all calibrated against this leverage.

2. ATR-adaptive stop loss. hourly_task now fetches the last ~20 hourly bars
   per symbol (fetch_recent_bars in pyquant_utils.py), computes a live ATR
   (compute_atr, Wilder's method), and passes it plus the current atr_mult
   (from optimal_params.json, written by the new Bayesian grid_search.py)
   into SignalGenerator.generate_signal(). SL price becomes
   entry ± max(atr_mult × ATR, floor) instead of the old fixed $225/$16
   offset — this is the exact mechanism validated in the backtest.

3. BTC and ETH now trade fully independently. The old code generated and
   could act on signals for both symbols every hour regardless of whether
   either already had an open position. This refactor adds an explicit
   check_open_position() gate per symbol BEFORE generating a new signal —
   a symbol with an open trade is skipped for new signal generation until
   it closes, while the OTHER symbol is never blocked by it. This matches
   the independent per-symbol design validated in pyquant_walkforward_v4.py
   (previously, a walkforward-side version of this same bug — `if in_b or
   in_e: continue` — was found and fixed there; this is the live-system
   equivalent of that fix).

4. Dynamic last-resort stop loss. The old fixed 12.5% / 10%-of-balance
   last-resort thresholds were calibrated against the old fixed $225/$16
   SL. With ATR-adaptive SL now potentially wider (or narrower) depending
   on market conditions, a fixed balance percentage could fire BEFORE the
   real bracket SL ever would, turning a backstop into a competing, tighter
   constraint. The last-resort threshold is now 1.5x the EXPECTED MAX LOSS
   implied by that specific signal's own SL distance (computed at signal
   generation time and stored in the signal dict), so it always scales with
   whatever SL width was actually used and only fires as a genuine backstop
   for bracket-placement failures, never as a routine competing exit.

5. Dead Bybit TP/SL code removed. The original architecture description
   named Bybit for TP/SL management, but that role has been fully replaced
   by Binance's Algo Order API (see PositionGuard._place_bracket_order in
   pyquant_utils.py) since Binance migrated all conditional orders off the
   old endpoint on 2025-12-09. The Bybit HTTP helpers (genSignature,
   HTTP_Request) and open_tp_sl_position() were never called anywhere in
   the live task flow after that migration and have been removed to reduce
   startup latency, failure surface, and complexity. The Bybit `session`
   client still exists in pyquant_utils.py for architectural compatibility
   / potential future use, but nothing in this file depends on it.

Everything else — env loading, shared clients, PositionGuard integration,
duplicate-run guards, the manual 1-minute tracker, virtual TP/SL safeguard,
signal_executor's 3-tuple return — is carried forward unchanged from the
fixes already validated in pyq_p3.py.
"""

# ── Standard library ──────────────────────────────────────────────────────────
import os
import json
import math
import time
import logging
import threading
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_FLOOR
from queue import Queue
from typing import Dict, Any, Set

# ── Third-party ───────────────────────────────────────────────────────────────
import numpy as np
import pandas as pd
import asyncio
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

from binance.client import Client
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from sklearn.metrics import (
    mean_squared_error, mean_absolute_percentage_error,
    mean_absolute_error, r2_score
)

from pyquant_utils import (
    UKFModel, get_equities, SignalGenerator, PositionGuard,
    data_download, aggregate_ohlcv_data,
    check_open_position, close_futures_position,
    compute_atr, fetch_recent_bars,
    # Shared clients — created once in pyquant_utils with retry logic.
    # NOTE: the Bybit `session` client is intentionally NOT imported here —
    # TP/SL management is fully handled by Binance's Algo Order API via
    # PositionGuard (see pyquant_utils.py), so nothing in this file needs it.
    binance_client, crypto_client, _binance_lock,
)

nest_asyncio.apply()

# ── Environment ───────────────────────────────────────────────────────────────
from pathlib import Path as _Path
_ENV_PATH = _Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=True)


def _require_env(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        raise EnvironmentError(
            f"Required variable '{key}' not set. "
            f"Check your .env file at: {_ENV_PATH}"
        )
    return value


ALPACA_API_KEY     = _require_env("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = _require_env("ALPACA_SECRET_KEY")
BYBIT_API_KEY      = _require_env("BYBIT_API_KEY")
BYBIT_SECRET_KEY   = _require_env("BYBIT_SECRET_KEY")
BINANCE_API_KEY    = _require_env("BINANCE_API_KEY")
BINANCE_SECRET_KEY = _require_env("BINANCE_SECRET_KEY")
TELEGRAM_TOKEN     = _require_env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = _require_env("TELEGRAM_CHAT_ID")

# ── Trading constant ──────────────────────────────────────────────────────────
# CHANGED from 50 to 10 — matches the validated walk-forward backtest exactly.
# Every SL/last-resort threshold in this file assumes this value.
LEVERAGE = 10

# ATR configuration — must match pyquant_walkforward_v4.py / grid_search.py
ATR_LOOKBACK_HOURS = 20   # bars fetched; only need 15 for a 14-period ATR,
                          # 20 gives a buffer against a thin/missing bar
ATR_PERIOD         = 14

# ── Clients ───────────────────────────────────────────────────────────────────
api = tradeapi.REST(
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
    "https://data.alpaca.markets/v1beta3/crypto/us/bars"
)

start_date    = dt.date.today() - dt.timedelta(days=60)
end_date      = dt.date.today()

crypto_stream  = CryptoDataStream(api_key=ALPACA_API_KEY, secret_key=ALPACA_SECRET_KEY)

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_console_h = logging.StreamHandler()
_console_h.setLevel(logging.INFO)
_file_h = logging.FileHandler("pyq_p4.log")
_file_h.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
_console_h.setFormatter(_fmt)
_file_h.setFormatter(_fmt)
if not logger.handlers:
    logger.addHandler(_console_h)
    logger.addHandler(_file_h)

logger.info("\nStarting PyQuant p4" + "-" * 80 + "\n")

# ── UKF model setup ───────────────────────────────────────────────────────────
ukf_model = UKFModel(crypto_client, start_date)

eth_symbols = ["ETH/USD"]
btc_symbols = ["BTC/USD"]

btc_ukf,      btc_high_ukf,      btc_low_ukf      = ukf_model.create_ukf_models("1H",  btc_symbols)
btc_fift_ukf, btc_fift_high_ukf, btc_fift_low_ukf = ukf_model.create_ukf_models("15T", btc_symbols)
btc_five_ukf, btc_five_high_ukf, btc_five_low_ukf = ukf_model.create_ukf_models("5T",  btc_symbols)

eth_ukf,      eth_high_ukf,      eth_low_ukf       = ukf_model.create_ukf_models("1H",  eth_symbols)
eth_fift_ukf, eth_fift_high_ukf, eth_fift_low_ukf  = ukf_model.create_ukf_models("15T", eth_symbols)
eth_five_ukf, eth_five_high_ukf, eth_five_low_ukf  = ukf_model.create_ukf_models("5T",  eth_symbols)

# ── Signal generator ──────────────────────────────────────────────────────────
signal_generator = SignalGenerator()

# ── PositionGuard ──────────────────────────────────────────────────────────────
guard = PositionGuard(
    binance_client=binance_client,
    telegram_token=TELEGRAM_TOKEN,
    chat_id=TELEGRAM_CHAT_ID,
)
guard.start()

# ── UKF mapping ───────────────────────────────────────────────────────────────
symbols_list = ["ETH/USD", "BTC/USD"]

ukf_mapping = {
    "ETH/USD": {"ukf": eth_ukf, "high_ukf": eth_high_ukf, "low_ukf": eth_low_ukf},
    "BTC/USD": {"ukf": btc_ukf, "high_ukf": btc_high_ukf, "low_ukf": btc_low_ukf},
}

# ── Module-level state ────────────────────────────────────────────────────────
predictions      = []
high_predictions = []
low_predictions  = []
ukf_handle       = []
signals          = []
order_changes    = []
order_history    = []

ordered         = False
sig_gened       = False
in_trade        = False
double_order    = False
order_changed   = False
position_closed = False
stake           = 0.9985

wallet_balance = None
btc_balance    = None
eth_balance    = None

btc_ordered_signal = None
eth_ordered_signal = None
btc_position       = False
eth_position       = False
bo_declared        = False
eo_declared        = False

btc_tracker = {
    "sig_gened": False, "ordered": False, "order_filled": False,
    "in_trade": False, "open_trade": False, "last_event_timestamp": None,
    "tp_hit": False, "sl_hit": False,
    # Frozen balance snapshot at order-placement time (unchanged from p3).
    "entry_balance": None,
    # NEW — the dollar loss expected if THIS signal's own SL is hit, computed
    # at signal-generation time from its actual (fixed or ATR-based) SL
    # distance. Drives the dynamic last-resort check below instead of a
    # fixed % of balance.
    "expected_max_loss": None,
}
eth_tracker = {
    "sig_gened": False, "ordered": False, "order_filled": False,
    "in_trade": False, "open_trade": False, "last_event_timestamp": None,
    "tp_hit": False, "sl_hit": False,
    "entry_balance": None,
    "expected_max_loss": None,
}

# ── Last-resort stop loss constants ──────────────────────────────────────────
# CHANGED: no longer a fixed % of entry_balance (that was calibrated against
# the old fixed $225/$16 SL and could fire before a wider ATR-based bracket
# SL ever would). The multiplier below is applied to each trade's OWN
# expected_max_loss (stored per-trade in the tracker), so the last-resort
# always scales with whatever SL width was actually used this trade.
LAST_RESORT_MULTIPLIER = 1.5
HARD_DOLLAR_SL_FLOOR    = -0.20   # absolute backstop regardless of position size

# ── Duplicate-run guards ──────────────────────────────────────────────────────
LAST_RUN_FILE   = "last_execution_timestamp.txt"
MINUTE_RUN_FILE = "minute_execution_timestamp.txt"


def has_function_run_this_hour() -> bool:
    if os.path.exists(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE, "r") as f:
                last = datetime.fromisoformat(f.read().strip())
            now = datetime.now()
            if (now.year, now.month, now.day, now.hour) == \
               (last.year, last.month, last.day, last.hour):
                return True
        except Exception as e:
            print(f"Error reading hour timestamp: {e}")
    return False


def update_last_run_timestamp():
    with open(LAST_RUN_FILE, "w") as f:
        f.write(datetime.now().isoformat())


def has_function_run_this_minute() -> bool:
    if os.path.exists(MINUTE_RUN_FILE):
        try:
            with open(MINUTE_RUN_FILE, "r") as f:
                last = datetime.fromisoformat(f.read().strip())
            now = datetime.now()
            if (now.year, now.month, now.day, now.hour, now.minute) == \
               (last.year, last.month, last.day, last.hour, last.minute):
                return True
        except Exception as e:
            print(f"Error reading minute timestamp: {e}")
    return False


def update_minute_run_timestamp():
    with open(MINUTE_RUN_FILE, "w") as f:
        f.write(datetime.now().isoformat())


# ── Utility helpers ───────────────────────────────────────────────────────────

def safe_decimal(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(value)
    except Exception:
        return Decimal("0")


def custom_floor(value: float, decimal_places: int) -> float:
    decimal_value = Decimal(str(value))
    quantum_str   = "1e-" + str(decimal_places)
    return float(decimal_value.quantize(Decimal(quantum_str), rounding=ROUND_FLOOR))


def handle_api_response(response: dict, action_description: str):
    if response and response.get("retCode") == 0:
        return response["result"]
    err = (
        f"Failed to {action_description}: "
        f"{response.get('retMsg', 'Unknown error')} "
        f"(retCode: {response.get('retCode', 'N/A')})"
    )
    logger.error(err)
    return None


# ── ATR fetch helper ──────────────────────────────────────────────────────────

def get_live_atr(symbol_alpaca: str) -> float:
    """
    Fetches the last ATR_LOOKBACK_HOURS of hourly bars and computes ATR.
    Returns 0.0 on any failure — SignalGenerator._calculate_sl_price falls
    back to the fixed $225/$16 offset when atr=0.0, so a fetch failure
    degrades to the old fixed-SL behaviour rather than blocking the signal
    or leaving a trade without a stop loss.
    """
    try:
        df = fetch_recent_bars(symbol_alpaca, hours=ATR_LOOKBACK_HOURS)
        if df is None or len(df) < 2:
            logger.warning(f"get_live_atr: insufficient bars for {symbol_alpaca} "
                           f"— falling back to fixed SL offset.")
            return 0.0
        return compute_atr(df, period=ATR_PERIOD)
    except Exception as e:
        logger.error(f"get_live_atr ({symbol_alpaca}): {e}")
        return 0.0


# ── signal_executor ───────────────────────────────────────────────────────────

def signal_executor(signal: dict, trade_val: float):
    """
    Places a Binance Futures entry order from a signal dict.
    Always returns a 3-tuple: (signal, response, ordered).
    """
    global ordered, signals
    response = None

    try:
        binance_client.futures_change_leverage(symbol=signal["symbol"], leverage=LEVERAGE)
        logger.info(f"SignalExecutor: leverage={LEVERAGE}x for {signal['symbol']}")
    except Exception as e:
        logger.warning(f"SignalExecutor: could not set leverage (may already be set): {e}")

    if not signal or signal.get("entry_price") is None:
        logger.warning("SignalExecutor: invalid signal – no order placed.")
        ordered = False
        return signal, response, ordered

    symbol      = signal["symbol"]
    order_side  = signal["order_side"].upper()
    order_type  = signal["order_type"].upper()
    entry_price = float(signal["entry_price"])

    logger.info(f"SignalExecutor: {symbol} {order_side} {order_type} @ {entry_price}")

    try:
        exchange_info = binance_client.futures_exchange_info()
        symbol_info   = next(
            (s for s in exchange_info["symbols"] if s["symbol"] == symbol), None
        )
        if symbol_info is None:
            logger.error(f"SignalExecutor: {symbol} not found in exchange info.")
            ordered = False
            return signal, response, ordered

        lot_filter  = next(f for f in symbol_info["filters"] if f["filterType"] == "LOT_SIZE")
        step_size   = float(lot_filter["stepSize"])
        ss_str      = str(step_size)
        precision   = len(ss_str.split(".")[1].rstrip("0")) if "." in ss_str else 0

        notional_value = trade_val * LEVERAGE
        raw_qty        = notional_value / entry_price
        quantity       = math.floor(raw_qty / step_size) * step_size
        quantity       = round(quantity, precision)
        quantity_str   = f"{quantity:.{precision}f}"
        signal["qty"]  = quantity_str
        logger.debug(f"SignalExecutor: qty={quantity_str}")
    except Exception as e:
        logger.error(f"SignalExecutor: qty calculation failed for {symbol}: {e}")
        ordered = False
        return signal, response, ordered

    order_params: dict = {
        "symbol": symbol, "side": order_side,
        "type": order_type, "quantity": quantity_str,
    }

    if order_type == "LIMIT":
        try:
            pf         = next(f for f in symbol_info["filters"] if f["filterType"] == "PRICE_FILTER")
            tick_size  = float(pf["tickSize"])
            ts_str     = str(tick_size)
            price_prec = len(ts_str.split(".")[1].rstrip("0")) if "." in ts_str else 0
            entry_price = round(entry_price / tick_size) * tick_size
            entry_str   = f"{entry_price:.{price_prec}f}"
        except StopIteration:
            entry_str = str(entry_price)
        order_params["price"]       = entry_str
        order_params["timeInForce"] = "GTC"

    try:
        logger.info(f"SignalExecutor: placing {order_type} for {symbol}…")
        response = binance_client.futures_create_order(**order_params)
        logger.info(f"SignalExecutor: order placed – ID={response['orderId']}")
        signal["orderId"] = response["orderId"]
        signals.append(signal)
        ordered = True
        return signal, response, ordered
    except Exception as e:
        logger.error(f"SignalExecutor: order placement failed for {symbol}: {e}")
        ordered = False
        return signal, response, ordered


# ── price_tracker / order_editor (kept for compatibility, not in default flow) ─

def price_tracker(signal, pred, high_pred, low_pred, price, the_symbol):
    """Detects entry drift and updates signal if price conditions shift."""
    try:
        if not isinstance(signal, dict):
            raise TypeError(f"Invalid signal format: {type(signal)}")
    except (TypeError, ValueError):
        raise TypeError(f"Could not interpret signal as dict: {type(signal)}")

    global order_changed, order_changes, stake, order_history, position_closed

    new_entry = new_sl = order_id = None
    symbol    = signal["symbol"]

    if ordered and not order_changed:
        order_id = signal.get("orderLinkId")
        if the_symbol == signal["symbol"]:
            if signal["order_side"] == "Buy":
                if low_pred < signal["entry_price"] and low_pred <= signal["sl_price"]:
                    new_entry              = low_pred
                    positions              = 10 / new_entry
                    new_sl                 = (positions * new_entry * stake) / positions
                    signal["entry_price"]  = new_entry
                    signal["sl_price"]     = new_sl
                    order_changed          = True
            elif signal["order_side"] == "Sell" and not order_changed:
                if high_pred > signal["entry_price"] and high_pred >= signal["sl_price"]:
                    new_entry              = high_pred
                    positions              = 10 / new_entry
                    new_sl                 = (positions * new_entry * (2 - stake)) / positions
                    signal["entry_price"]  = new_entry
                    signal["sl_price"]     = new_sl
                    order_changed          = True

            if signal["order_side"] == "Buy" and not position_closed:
                if price["high"] >= signal["tp_price"]:
                    signal["condition"] = "Profit"
                elif price["low"] <= signal["sl_price"]:
                    signal["condition"] = "Loss"
            elif signal["order_side"] == "Sell" and not position_closed:
                if price["low"] <= signal["tp_price"]:
                    signal["condition"] = "Profit"
                elif price["high"] >= signal["sl_price"]:
                    signal["condition"] = "Loss"

    order_changes.append({
        "new_entry": new_entry, "new_sl": new_sl,
        "order_id": order_id,  "symbol": symbol,
    })
    if order_changed:
        return order_changes[-1]
    order_changed = False
    return None


def order_editor(order_changes: dict):
    logger.info(
        f"[OrderEditor] symbol={order_changes['symbol']}  "
        f"orderId={order_changes['order_id']}  "
        f"new_entry={order_changes['new_entry']}  "
        f"new_sl={order_changes['new_sl']}"
    )


# ── position_manager ──────────────────────────────────────────────────────────

def position_manager(symbol: str, data):
    """Checks open position against UKF trend/pct_diff. Closes on reversal or loss limit."""
    global in_trade, signals, eth_tracker, btc_tracker

    logger.info(f"Starting Position Manager for {symbol}")
    try:
        position_status = check_open_position(symbol=symbol)
        if position_status["is_open_position"]:
            trend    = signal_generator.trend
            pct_diff = signal_generator.pct_diff
            side     = position_status["side"].lower()

            if side == "buy" and trend == "down":
                if pct_diff is not None and pct_diff >= 0.00005 \
                        and position_status["unrealized_pnl"] > 0:
                    close_futures_position(position_status)
                    logger.info(f"PM: closed {symbol} BUY on downtrend reversal. "
                                f"PNL={position_status['unrealized_pnl']:.4f}")
                elif position_status["unrealized_pnl"] <= -0.2:
                    close_futures_position(position_status)
                    logger.info(f"PM: closed {symbol} BUY on loss limit.")

            elif side == "sell" and trend == "up":
                if pct_diff is not None and pct_diff >= 0.00005 \
                        and position_status["unrealized_pnl"] > 0:
                    close_futures_position(position_status)
                    logger.info(f"PM: closed {symbol} SELL on uptrend reversal. "
                                f"PNL={position_status['unrealized_pnl']:.4f}")
                elif position_status["unrealized_pnl"] <= -0.2:
                    close_futures_position(position_status)
                    logger.info(f"PM: closed {symbol} SELL on loss limit.")

            sig = None
            if signals and symbol == signals[-1]["symbol"] and not signals[-1].get("checked"):
                signals[-1]["checked"] = True
                sig = signals[-1]
            elif len(signals) >= 2 and symbol == signals[-2]["symbol"] \
                    and not signals[-2].get("checked"):
                signals[-2]["checked"] = True
                sig = signals[-2]

            if sig and data["high"] > sig["tp_price"]:
                close_futures_position(position_status)
                logger.info(f"PM2: {symbol} TP already passed post-fill – closed. "
                            f"PNL={position_status['unrealized_pnl']:.4f}")

        print(f"position_manager: finished for {symbol}")
    except Exception as e:
        logger.error(f"position_manager ({symbol}): {e}")


# ── Dynamic last-resort stop loss ────────────────────────────────────────────

def last_resort_stop_loss_check(symbol: str, tracker: dict) -> bool:
    """
    Final safety net, checked every minute.

    CHANGED from a fixed % of entry_balance to a DYNAMIC threshold:
    1.5 x tracker['expected_max_loss'], where expected_max_loss is the
    dollar loss THIS SPECIFIC TRADE would realize if its own SL price were
    hit — computed at signal-generation time using its actual SL distance
    (whether ATR-based or the fixed fallback).

    Why this changed: the old fixed 12.5%/10%-of-balance thresholds were
    calibrated against the old fixed $225 (BTC) / $16 (ETH) SL offsets. With
    ATR-adaptive SL now potentially much wider during volatile periods, a
    fixed balance percentage could be tighter in dollar terms than the
    bracket SL itself — meaning this "last resort" would routinely fire
    BEFORE the real bracket order ever would, turning a backstop into a
    competing, premature exit. Scaling the threshold to each trade's own
    expected_max_loss (with a 1.5x safety margin) guarantees this check
    only fires as a genuine backstop for bracket-placement failures.

    HARD_DOLLAR_SL_FLOOR remains as an absolute backstop for the case where
    expected_max_loss wasn't stored (e.g. an untracked position picked up
    from the exchange with no known signal history).
    """
    expected_max_loss = tracker.get("expected_max_loss")

    try:
        position_status = check_open_position(symbol=symbol)
        if not position_status["is_open_position"]:
            return False

        upnl = position_status["unrealized_pnl"]
        if upnl >= 0:
            return False

        dynamic_breach = (expected_max_loss is not None and expected_max_loss > 0
                          and abs(upnl) >= LAST_RESORT_MULTIPLIER * expected_max_loss)
        hard_breach = upnl <= HARD_DOLLAR_SL_FLOOR

        if dynamic_breach or hard_breach:
            reason = (
                f"{LAST_RESORT_MULTIPLIER}x expected max loss (${expected_max_loss:.4f})"
                if dynamic_breach else
                f"hard dollar floor (${HARD_DOLLAR_SL_FLOOR:.2f})"
            )
            logger.warning(
                f"🛑 Last-resort SL triggered for {symbol}: uPNL=${upnl:.4f} "
                f"breached {reason}. Closing position."
            )
            close_futures_position(position_status)
            guard.add_alert_to_queue(
                f"🛑 Last-resort SL closed {symbol}  uPNL=${upnl:.4f}  ({reason})"
            )
            return True

    except Exception as e:
        logger.error(f"last_resort_stop_loss_check ({symbol}): {e}")

    return False


# ── hourly_task ───────────────────────────────────────────────────────────────

def hourly_task():
    if has_function_run_this_hour():
        print(f"Hourly task already ran this hour. Skipping.")
        return

    update_last_run_timestamp()

    global ordered, sig_gened, order_changed, signal, my_signal, signals
    global eth_tracker, btc_tracker, position_closed
    global wallet_balance
    global btc_ordered_signal, eth_ordered_signal
    global bo_declared, eo_declared, in_trade, double_order
    global btc_balance, eth_balance

    sig_gened               = False
    signal_generator.sig_gened = False
    ordered                 = False
    bo_declared             = False
    eo_declared             = False
    my_signal               = None
    btc_ordered_signal      = None
    eth_ordered_signal      = None
    btc_signal              = None
    eth_signal              = None
    btc_response            = None
    eth_response            = None
    btc_ordered             = False
    eth_ordered             = False

    btc_tracker.update({"sig_gened": False, "ordered": False, "order_filled": False})
    eth_tracker.update({"sig_gened": False, "ordered": False, "order_filled": False})

    now = datetime.now()
    print(f"[{now:%Y-%m-%d %H:%M:%S}] Running Hourly Task.")

    # Balance
    try:
        wallet_balance = None
        while wallet_balance is None:
            try:
                usdt_equity, btc_equity, eth_equity = get_equities()
                wallet_balance = float(usdt_equity)
                btc_balance    = float(btc_equity)
                eth_balance    = float(eth_equity)
                logger.info(f"Balance – USDT:{wallet_balance}  BTC:{btc_balance}  ETH:{eth_balance}")
            except Exception as e:
                logger.error(f"Error getting balance: {e}")
                time.sleep(2)

        try:
            open_orders         = binance_client.futures_get_open_orders()
            symbols_with_orders = {o["symbol"] for o in open_orders}
            for sym in symbols_with_orders:
                binance_client.futures_cancel_all_open_orders(symbol=sym)
            logger.info("Cancelled all stale open orders.")
        except Exception as e:
            logger.error(f"Error cancelling open orders: {e}")

        if not wallet_balance:
            logger.warning("Balance unavailable – using default $3.")
            wallet_balance = 3.0

    except Exception as e:
        logger.error(f"Balance routine error: {e}")

    # Secondary last-resort backstop (primary check is the 1-minute task)
    try:
        btc_pos = check_open_position(symbol="BTCUSDT")
        if btc_pos["is_open_position"]:
            logger.info(
                f"Open BTC: side={btc_pos['side']}  size={btc_pos['size']}  "
                f"entry={btc_pos['entry_price']:.4f}  uPNL={btc_pos['unrealized_pnl']:.4f}"
            )
            in_trade = btc_tracker["in_trade"] = True
            if btc_tracker["open_trade"]:
                if last_resort_stop_loss_check("BTCUSDT", btc_tracker):
                    btc_tracker.update({"open_trade": False, "sl_hit": True,
                                         "entry_balance": None, "expected_max_loss": None})
                    in_trade = btc_tracker["in_trade"] = False
        else:
            in_trade = btc_tracker["in_trade"] = False

        eth_pos = check_open_position(symbol="ETHUSDT")
        if eth_pos["is_open_position"]:
            logger.info(
                f"Open ETH: side={eth_pos['side']}  size={eth_pos['size']}  "
                f"entry={eth_pos['entry_price']:.4f}  uPNL={eth_pos['unrealized_pnl']:.4f}"
            )
            in_trade = eth_tracker["in_trade"] = True
            if eth_tracker["open_trade"]:
                if last_resort_stop_loss_check("ETHUSDT", eth_tracker):
                    eth_tracker.update({"open_trade": False, "sl_hit": True,
                                         "entry_balance": None, "expected_max_loss": None})
                    in_trade = eth_tracker["in_trade"] = False
        else:
            in_trade = eth_tracker["in_trade"] = False

    except Exception as e:
        logger.error(f"Position check error: {e}")

    position_closed = False

    # Read the current atr_mult once per cycle (written daily by grid_search.py)
    atr_mult = signal_generator.load_params().get("atr_mult", 1.8)

    # Signal generation
    logger.info("Starting Hourly Signal Services")
    try:
        for symbol in symbols_list:
            trade_symbol = "BTCUSDT" if symbol in ("BTC/USD", "BTCUSDT") else "ETHUSDT"
            ukf_data = ukf_mapping.get(symbol)

            # FIX (independent per-symbol trading, matching the walk-forward
            # validation): a symbol that already has an open position is
            # skipped for NEW signal generation this hour. This never blocks
            # the OTHER symbol — BTC being open has no bearing on whether ETH
            # can open its own new trade this cycle, and vice versa. Without
            # this gate, a symbol could stack a second entry order on top of
            # an already-open position.
            already_open = check_open_position(symbol=trade_symbol)["is_open_position"]
            if already_open:
                logger.info(f"{symbol} already has an open position — "
                            f"skipping new signal generation this hour.")
                if symbol in ("BTC/USD", "BTCUSDT"):
                    btc_signal = None
                    btc_tracker.update({"sig_gened": False, "ordered": False})
                else:
                    eth_signal = None
                    eth_tracker.update({"sig_gened": False, "ordered": False})
                continue

            ddata = None
            while ddata is None:
                try:
                    downloaded_data = data_download("1H", symbol)
                    if downloaded_data is not None:
                        ddata = downloaded_data.iloc[-1]
                    else:
                        print(f"Data None for {symbol} (1H). Retrying…")
                        time.sleep(2)
                except Exception as e:
                    print(f"Error downloading {symbol} 1H: {e}")
                    time.sleep(2)

            price = ddata["close"]

            try:
                position_manager(symbol, ddata)
                if symbol in ("ETH/USD", "ETHUSDT"):
                    eth_ps = check_open_position(symbol="ETHUSDT")
                    if eth_ps["is_open_position"]:
                        hr_inc  = 1.003
                        net_val = (wallet_balance * hr_inc) * LEVERAGE
                        take_tp = net_val / eth_ps["size"]
                        if eth_ps["side"].lower() == "buy" and ddata["high"] >= take_tp:
                            close_futures_position(eth_ps)
                            logger.info("Hourly ETH BUY TP threshold hit – closed.")
                        elif eth_ps["side"].lower() == "sell" and ddata["low"] <= take_tp:
                            close_futures_position(eth_ps)
                            logger.info("Hourly ETH SELL TP threshold hit – closed.")
            except Exception as e:
                logger.error(f"position_manager / hourly threshold error: {e}")

            # NEW — fetch live ATR for this symbol before generating the signal
            atr_val = get_live_atr(symbol)

            if ukf_data:
                try:
                    pred, high_pred, low_pred = UKFModel.ukf_handler(
                        ddata,
                        ukf_data["ukf"], ukf_data["high_ukf"], ukf_data["low_ukf"]
                    )
                    my_signal = signal_generator.generate_signal(
                        price, pred, high_pred, low_pred, symbol, LEVERAGE, wallet_balance,
                        atr=atr_val, atr_mult=atr_mult
                    )
                except Exception as e:
                    print(f"Signal generation error for {symbol}: {e}")
            else:
                print(f"Warning: no UKF data for {symbol}")

            if my_signal:
                logger.info(f"{symbol} signal: {my_signal}")
                if symbol in ("BTC/USD", "BTCUSDT"):
                    btc_signal = my_signal
                    btc_tracker.update({"sig_gened": True, "ordered": False})
                elif symbol in ("ETH/USD", "ETHUSDT"):
                    eth_signal = my_signal
                    eth_tracker.update({"sig_gened": True, "ordered": False})
                else:
                    logger.error(f"Signal generated for unknown symbol: {symbol}")
                ordered = False
            else:
                print(f"No signal for {symbol} (1H).")
                signal_generator.sig_gened = False
                if symbol in ("BTC/USD", "BTCUSDT"):
                    btc_signal = None
                    btc_tracker.update({"sig_gened": False, "ordered": False})
                elif symbol in ("ETH/USD", "ETHUSDT"):
                    eth_signal = None
                    eth_tracker.update({"sig_gened": False, "ordered": False})

    except Exception as e:
        logger.error(f"Signal generation block error: {e}")

    # Order execution
    try:
        print("Starting Signal Execution block (1hr)")

        if btc_tracker["sig_gened"] and eth_tracker["sig_gened"]:
            logger.info("Signal from both coins.")
            trade_value = float(wallet_balance)

            try:
                eth_ordered_signal, eth_response, eth_ordered = signal_executor(eth_signal, trade_value)
                if eth_response:
                    guard.start_guard_for_order(eth_signal, eth_response["orderId"])
            except Exception as e:
                logger.error(f"ETH order execution error: {e}")

            if eth_ordered:
                logger.info(f"ETH order placed: id={eth_response.get('orderId') if eth_response else 'N/A'}")
            else:
                logger.info("ETH order not placed successfully.")

            double_order = True
            signal_generator.sig_gened = True
            ordered = True
            eth_tracker.update({"sig_gened": True, "last_event_timestamp": now,
                                 "entry_balance": wallet_balance,
                                 "expected_max_loss": eth_signal.get("expected_max_loss")})
            btc_tracker.update({"sig_gened": True, "last_event_timestamp": now,
                                 "entry_balance": wallet_balance,
                                 "expected_max_loss": btc_signal.get("expected_max_loss")})

        elif btc_tracker["sig_gened"] and not eth_tracker["sig_gened"]:
            trade_value = float(wallet_balance) * 0.8
            btc_ordered_signal, btc_response, btc_ordered = signal_executor(btc_signal, trade_value)
            try:
                if btc_response:
                    guard.start_guard_for_order(btc_signal, btc_response["orderId"])
            except Exception as e:
                logger.error(f"BTC guard start error: {e}")

            logger.info(f"BTC signal: {btc_ordered_signal}")
            logger.info(f"BTC order: id={btc_response.get('orderId') if btc_response else 'N/A'}")

            eth_ordered_signal = None
            signal_generator.sig_gened = True
            ordered      = True
            double_order = False
            btc_tracker.update({"ordered": True, "last_event_timestamp": now,
                                 "entry_balance": wallet_balance,
                                 "expected_max_loss": btc_signal.get("expected_max_loss")})
            eth_tracker["sig_gened"] = False

        elif eth_tracker["sig_gened"] and not btc_tracker["sig_gened"]:
            trade_value = float(wallet_balance) * 0.8
            eth_ordered_signal, eth_response, eth_ordered = signal_executor(eth_signal, trade_value)
            try:
                if eth_response:
                    guard.start_guard_for_order(eth_signal, eth_response["orderId"])
            except Exception as e:
                logger.error(f"ETH guard start error: {e}")

            logger.info(f"ETH signal: {eth_ordered_signal}")
            logger.info(f"ETH order: id={eth_response.get('orderId') if eth_response else 'N/A'}")

            btc_ordered_signal = None
            signal_generator.sig_gened = True
            ordered      = True
            double_order = False
            eth_tracker.update({"sig_gened": True, "ordered": True,
                                 "last_event_timestamp": now,
                                 "entry_balance": wallet_balance,
                                 "expected_max_loss": eth_signal.get("expected_max_loss")})
            btc_tracker["sig_gened"] = False

        else:
            print("No signals from either coin. No orders placed.")
            logger.info("No signal from either coin this hour.")
            btc_ordered_signal = eth_ordered_signal = None
            signal_generator.sig_gened = False
            btc_tracker.update({"sig_gened": False, "ordered": False})
            eth_tracker.update({"sig_gened": False, "ordered": False})

    except Exception as e:
        logger.error(f"Signal execution error: {e}\n{traceback.format_exc()}")


# ── fifteen_minute_task ───────────────────────────────────────────────────────

def fifteen_minute_task():
    global order_changed, my_signal, btc_ordered_signal, eth_ordered_signal

    try:
        the_symbol = None
        now        = datetime.now()
        print(f"[{now:%Y-%m-%d %H:%M:%S}] Running 15-minute task.")

        for i in symbols_list:
            the_symbol = i
            fift_data  = None
            while fift_data is None:
                try:
                    downloaded_data = data_download("1T", i)
                    if downloaded_data is not None:
                        agg       = aggregate_ohlcv_data(downloaded_data.copy(), aggregation_minutes=15)
                        agg.dropna(inplace=True)
                        fift_data = agg.iloc[-1]
                    else:
                        print(f"15M data None for {i}. Retrying…")
                        time.sleep(2)
                except Exception as e:
                    print(f"Error downloading 15M data for {i}: {e}")
                    time.sleep(2)

            print(f"{i}: Open:{fift_data['open']}  High:{fift_data['high']}  Low:{fift_data['low']}")

            if i in ("BTC/USD", ["BTC/USD"]):
                UKFModel.ukf_handler(fift_data, btc_fift_ukf, btc_fift_high_ukf, btc_fift_low_ukf)
            elif i in ("ETH/USD", ["ETH/USD"]):
                UKFModel.ukf_handler(fift_data, eth_fift_ukf, eth_fift_high_ukf, eth_fift_low_ukf)
            else:
                print(f"Problem generating 15T preds for {i}")

        if signal_generator.sig_gened:
            print("Work on Price Tracker (15T)")
        if order_changed:
            order_changed = False

    except Exception as e:
        print(f"15-minute task error: {e}\n{traceback.format_exc()}")


# ── five_minute_task ──────────────────────────────────────────────────────────

def five_minute_task():
    global order_changed, signal, my_signal

    try:
        for i in symbols_list:
            five_data = None
            while five_data is None:
                try:
                    downloaded_data = data_download("1T", i)
                    if downloaded_data is not None:
                        agg       = aggregate_ohlcv_data(downloaded_data.copy(), aggregation_minutes=5)
                        five_data = agg.iloc[-1]
                    else:
                        print(f"5M data None for {i}. Retrying…")
                        time.sleep(2)
                except Exception as e:
                    print(f"Error downloading 5M data for {i}: {e}")
                    time.sleep(2)

            print(f"{i}: O:{five_data['open']}  H:{five_data['high']}  "
                  f"L:{five_data['low']}  C:{five_data['close']}")

            if i in ("BTC/USD", ["BTC/USD"]):
                UKFModel.ukf_handler(five_data, btc_five_ukf, btc_five_high_ukf, btc_five_low_ukf)
            elif i in ("ETH/USD", ["ETH/USD"]):
                UKFModel.ukf_handler(five_data, eth_five_ukf, eth_five_high_ukf, eth_five_low_ukf)
            else:
                print(f"Problem generating 5T preds for {i}")

        if signal_generator.sig_gened:
            print("Work on Price Tracker (5T)")
        if order_changed:
            order_changed = False

    except Exception as e:
        print(f"5-minute task error: {e}\n{traceback.format_exc()}")


# ── one_minute_task ───────────────────────────────────────────────────────────

def one_minute_task():
    if has_function_run_this_minute():
        print(f"1-minute task already ran this minute. Skipping.")
        return

    update_minute_run_timestamp()

    global btc_ordered_signal, double_order, ordered, eth_ordered_signal
    global btc_tracker, eth_tracker
    global btc_balance, eth_balance, wallet_balance
    global bo_declared, eo_declared
    global btc_position, eth_position

    wallet_balance = None

    try:
        btc_position_status = check_open_position(symbol="BTCUSDT")
        eth_position_status = check_open_position(symbol="ETHUSDT")

        if btc_tracker["open_trade"] or eth_tracker["open_trade"]:
            try:
                usdt_eq, btc_eq, eth_eq = get_equities()
                wallet_balance = float(usdt_eq)
                btc_balance    = float(btc_eq)
                eth_balance    = float(eth_eq)
                if btc_tracker["open_trade"] and not btc_position:
                    btc_position = True
                if eth_tracker["open_trade"] and not eth_position:
                    eth_position = True
            except Exception as e:
                print(f"Equity fetch error (1m): {e}")

        elif btc_position_status["is_open_position"] or eth_position_status["is_open_position"]:
            logger.info("Untracked open position detected on exchange.")
            try:
                usdt_eq, btc_eq, eth_eq = get_equities()
                wallet_balance = float(usdt_eq)
                btc_balance    = float(btc_eq)
                eth_balance    = float(eth_eq)
                if btc_position_status["is_open_position"] and not btc_position:
                    btc_position = True
                    btc_tracker["open_trade"] = True
                    if not btc_tracker.get("entry_balance"):
                        btc_tracker["entry_balance"] = wallet_balance
                    if not btc_tracker.get("expected_max_loss"):
                        # No signal history for this untracked position — fall
                        # back to the hard dollar floor only (dynamic_breach
                        # in last_resort_stop_loss_check requires a positive
                        # expected_max_loss, so leaving this None is safe and
                        # simply skips straight to the hard floor check).
                        btc_tracker["expected_max_loss"] = None
                if eth_position_status["is_open_position"] and not eth_position:
                    eth_position = True
                    eth_tracker["open_trade"] = True
                    if not eth_tracker.get("entry_balance"):
                        eth_tracker["entry_balance"] = wallet_balance
                    if not eth_tracker.get("expected_max_loss"):
                        eth_tracker["expected_max_loss"] = None
            except Exception as e:
                print(f"Equity fetch error (untracked 1m): {e}")
        else:
            btc_position = eth_position = False

        now = datetime.now()
        print(f"[{now:%Y-%m-%d %H:%M:%S}] Running 1-minute task.")

        for i in symbols_list:
            data = None
            while data is None:
                try:
                    downloaded_data = data_download("1T", i)
                    if downloaded_data is not None:
                        data     = downloaded_data
                        one_data = data.iloc[-1]
                    else:
                        print(f"1M data None for {i}. Retrying…")
                        time.sleep(2)
                except Exception as e:
                    print(f"Error downloading 1M data for {i}: {e}")
                    time.sleep(2)

            price = one_data["close"]
            llow  = one_data["low"]
            hhigh = one_data["high"]
            print(f"{i}: O:{one_data['open']}  H:{hhigh}  L:{llow}  C:{price}")

            # PositionGuard virtual TP/SL safeguard
            if guard and guard.monitored_orders:
                for order_id, order_info in list(guard.monitored_orders.items()):
                    if (order_info["symbol"] == i or
                            order_info["symbol"].replace("/", "") == i.replace("/", "")):
                        sig = order_info.get("signal")
                        if not sig:
                            continue
                        virtual_close = False
                        reason        = ""
                        if sig["order_side"] == "Buy":
                            if hhigh >= sig["tp_price"]:
                                virtual_close = True; reason = "Virtual TP (Buy)"
                            elif llow <= sig["sl_price"]:
                                virtual_close = True; reason = "Virtual SL (Buy)"
                        elif sig["order_side"] == "Sell":
                            if llow <= sig["tp_price"]:
                                virtual_close = True; reason = "Virtual TP (Sell)"
                            elif hhigh >= sig["sl_price"]:
                                virtual_close = True; reason = "Virtual SL (Sell)"

                        if virtual_close:
                            logger.warning(f"🚨 {reason} HIT for {i}. Checking position…")
                            pos_check = check_open_position(symbol=order_info["symbol"])
                            if pos_check["is_open_position"]:
                                logger.warning("Position OPEN – executing emergency close.")
                                close_futures_position(pos_check)
                                guard.add_alert_to_queue(
                                    f"✅ {reason} executed via 1m safeguard for {i}"
                                )
                                del guard.monitored_orders[order_id]
                                guard.save_to_json()
                                if order_info["symbol"] == "BTCUSDT":
                                    btc_tracker.update({"open_trade": False,
                                                         "entry_balance": None,
                                                         "expected_max_loss": None})
                                elif order_info["symbol"] == "ETHUSDT":
                                    eth_tracker.update({"open_trade": False,
                                                         "entry_balance": None,
                                                         "expected_max_loss": None})

            # Dynamic last-resort PnL-based stop loss (see docstring above)
            if i in ("BTC/USD", "BTCUSDT") and btc_tracker["open_trade"]:
                if last_resort_stop_loss_check("BTCUSDT", btc_tracker):
                    btc_tracker.update({"open_trade": False, "sl_hit": True,
                                         "entry_balance": None, "expected_max_loss": None})

            if i in ("ETH/USD", "ETHUSDT") and eth_tracker["open_trade"]:
                if last_resort_stop_loss_check("ETHUSDT", eth_tracker):
                    eth_tracker.update({"open_trade": False, "sl_hit": True,
                                         "entry_balance": None, "expected_max_loss": None})

            # BTC manual tracker
            if i in ("BTC/USD", "BTCUSDT"):
                try:
                    bo_signal = btc_ordered_signal
                    if bo_signal is not None:
                        btc_position_status = check_open_position(symbol="BTCUSDT")
                        if not btc_tracker["order_filled"] and btc_tracker["sig_gened"]:
                            if bo_signal["order_type"] == "Market":
                                btc_tracker.update({"order_filled": True, "open_trade": True})
                                logger.info(f"{i} market order filled. Price:{price}")
                            if bo_signal["current_price"] > bo_signal["entry_price"] \
                                    and price <= bo_signal["entry_price"]:
                                btc_tracker.update({"order_filled": True, "open_trade": True})
                                logger.info(f"{i} BUY limit filled. Price:{price}")
                            elif bo_signal["current_price"] < bo_signal["entry_price"] \
                                    and price >= bo_signal["entry_price"]:
                                btc_tracker.update({"order_filled": True, "open_trade": True})
                                logger.info(f"{i} SELL limit filled. Price:{price}")

                        if btc_tracker["open_trade"] \
                                and not btc_tracker["tp_hit"] \
                                and not btc_tracker["sl_hit"]:
                            pnl = float(bo_signal["current_bal"]) - (wallet_balance or 0)
                            if bo_signal["order_side"] == "Buy":
                                if hhigh >= bo_signal["tp_price"]:
                                    btc_tracker.update({"tp_hit": True, "open_trade": False,
                                                         "entry_balance": None, "expected_max_loss": None})
                                    logger.info(f"{i} TP hit. Price:{price}  PNL:{pnl}")
                                elif llow <= bo_signal["sl_price"]:
                                    btc_tracker.update({"sl_hit": True, "open_trade": False,
                                                         "entry_balance": None, "expected_max_loss": None})
                                    logger.info(f"{i} SL hit. Price:{price}  PNL:{pnl}")
                            elif bo_signal["order_side"] == "Sell":
                                if llow <= bo_signal["tp_price"]:
                                    btc_tracker.update({"tp_hit": True, "open_trade": False,
                                                         "entry_balance": None, "expected_max_loss": None})
                                    logger.info(f"{i} TP hit. Price:{price}  PNL:{pnl}")
                                elif hhigh >= bo_signal["sl_price"]:
                                    btc_tracker.update({"sl_hit": True, "open_trade": False,
                                                         "entry_balance": None, "expected_max_loss": None})
                                    logger.info(f"{i} SL hit. Price:{price}  PNL:{pnl}")
                            else:
                                print("BTC order tracking: unknown order_side.")

                    elif bo_signal is None and btc_tracker["sig_gened"] and not bo_declared:
                        logger.info("BTC sig_gened=True but no ordered_signal – order likely failed.")
                        bo_declared = True

                except Exception as e:
                    print(f"BTC 1m tracker error: {e}")

            # ETH manual tracker
            if i in ("ETH/USD", "ETHUSDT"):
                try:
                    eo_signal = eth_ordered_signal
                    if eo_signal is not None:
                        eth_position_status = check_open_position(symbol="ETHUSDT")
                        if not eth_tracker["order_filled"] and eth_tracker["sig_gened"]:
                            if eo_signal["order_type"] == "Market":
                                eth_tracker.update({"order_filled": True, "open_trade": True})
                                logger.info(f"{i} market order filled. Price:{price}")
                            if eo_signal["current_price"] > eo_signal["entry_price"] \
                                    and price <= eo_signal["entry_price"]:
                                eth_tracker.update({"order_filled": True, "open_trade": True})
                                logger.info(f"{i} BUY limit filled. Price:{price}")
                            elif eo_signal["current_price"] < eo_signal["entry_price"] \
                                    and price >= eo_signal["entry_price"]:
                                eth_tracker.update({"order_filled": True, "open_trade": True})
                                logger.info(f"{i} SELL limit filled. Price:{price}")

                        if eth_tracker["open_trade"] \
                                and not eth_tracker["tp_hit"] \
                                and not eth_tracker["sl_hit"]:
                            pnl = float(eo_signal["current_bal"]) - (wallet_balance or 0)
                            if eo_signal["order_side"] == "Buy":
                                if hhigh >= eo_signal["tp_price"]:
                                    eth_tracker.update({"tp_hit": True, "open_trade": False,
                                                         "entry_balance": None, "expected_max_loss": None})
                                    logger.info(f"{i} TP hit. Price:{price}  PNL:{pnl}")
                                elif llow <= eo_signal["sl_price"]:
                                    eth_tracker.update({"sl_hit": True, "open_trade": False,
                                                         "entry_balance": None, "expected_max_loss": None})
                                    logger.info(f"{i} SL hit. Price:{price}  PNL:{pnl}")
                            elif eo_signal["order_side"] == "Sell":
                                if llow <= eo_signal["tp_price"]:
                                    eth_tracker.update({"tp_hit": True, "open_trade": False,
                                                         "entry_balance": None, "expected_max_loss": None})
                                    logger.info(f"{i} TP hit. Price:{price}  PNL:{pnl}")
                                elif hhigh >= eo_signal["sl_price"]:
                                    eth_tracker.update({"sl_hit": True, "open_trade": False,
                                                         "entry_balance": None, "expected_max_loss": None})
                                    logger.info(f"{i} SL hit. Price:{price}  PNL:{pnl}")
                            else:
                                print("ETH order tracking: unknown order_side.")

                    elif eo_signal is None and eth_tracker["sig_gened"] and not eo_declared:
                        logger.info("ETH sig_gened=True but no ordered_signal – order likely failed.")
                        eo_declared = True

                except Exception as e:
                    print(f"ETH 1m tracker error: {e}")

        print("1m task executed!")

    except Exception as e:
        print(f"1-minute task fatal error: {e}\n{traceback.format_exc()}")
