"""
pyq_p4.py
PyQuant live trading module — complete unabridged base containing all 18 UKF models
(1H, 15M, 5M), full retry loops, legacy 1-minute trackers, daily 1H UKF retraining,
Test Mode, and Watchdog virtual trade tracking.

MODES:
  TEST_MODE     = False  (If True, generates signals but bypasses live order execution)
  WATCHDOG_MODE = True   (If True, monitors all signals from entry fill to TP/SL exit)
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
from typing import Dict, Any, Set, Optional

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

from p4_utils import (
    UKFModel, build_ukfs, step_ukf, get_equities, SignalGenerator, PositionGuard,
    data_download, aggregate_ohlcv_data,
    check_open_position, close_futures_position,
    compute_atr, fetch_recent_bars,
    binance_client, crypto_client, _binance_lock,
    PYQUANT_MODE,
)
import pyquant_signal_math as signal_math
import pyquant_state as state_store
import pyquant_resilience as resilience

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

# ── Trading & Operational Constants ──────────────────────────────────────────
# Sourced from pyquant_signal_math (Phase 0 unification) so live and
# walkforward.py can't silently drift on leverage again.
LEVERAGE           = signal_math.LEVERAGE_DEFAULT
ATR_LOOKBACK_HOURS = 20
ATR_PERIOD         = 14
UKF_TRAIN_DAYS     = 30

TEST_MODE     = False  # Set to True to bypass live order execution on Binance
WATCHDOG_MODE = True   # Set to True to track all signals bar-by-bar to TP/SL

# SIGNAL_ONLY_MODE: env-driven (not hardcoded, unlike TEST_MODE/WATCHDOG_MODE
# above) so it can be turned on/off via a GitHub Actions repository
# Variable — the same mechanism already used for PYQUANT_MODE — without a
# code change or redeploy. When true, hourly_task() takes a third branch
# ahead of the existing TEST_MODE/trading_enabled checks: it generates and
# LOGS signals (via simulate_signal_entry(), see below) and still runs the
# existing one_minute_task() tracker's simulated TP/SL check against real
# market data, but makes ZERO calls to any exchange client — not
# futures_create_order(), not futures_change_leverage(), not even
# futures_exchange_info(). This is distinct from TEST_MODE, whose current
# implementation only logs a message and does nothing further (see DOCS.md
# Section 9 for why TEST_MODE alone could not satisfy this requirement —
# log_signal() is called from inside signal_executor(), which TEST_MODE
# skips entirely). SIGNAL_ONLY_MODE takes priority over TEST_MODE and the
# trading_enabled UI switch: if it's on, no order is ever placed,
# regardless of either of those.
SIGNAL_ONLY_MODE = os.environ.get("SIGNAL_ONLY_MODE", "false").strip().lower() in ("1", "true", "yes")

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

logger.info("\nStarting PyQuant p4 (Full Base + Daily UKF Retrain + Watchdog)" + "-" * 40 + "\n")

# ── UKF Models Setup (All 18 Models Initialized at Startup) ───────────────────
ukf_model = UKFModel(crypto_client, start_date)

eth_symbols = ["ETH/USD"]
btc_symbols = ["BTC/USD"]

btc_ukf,      btc_high_ukf,      btc_low_ukf      = ukf_model.create_ukf_models("1H",  btc_symbols)
btc_fift_ukf, btc_fift_high_ukf, btc_fift_low_ukf = ukf_model.create_ukf_models("15T", btc_symbols)
btc_five_ukf, btc_five_high_ukf, btc_five_low_ukf = ukf_model.create_ukf_models("5T",  btc_symbols)

eth_ukf,      eth_high_ukf,      eth_low_ukf       = ukf_model.create_ukf_models("1H",  eth_symbols)
eth_fift_ukf, eth_fift_high_ukf, eth_fift_low_ukf  = ukf_model.create_ukf_models("15T", eth_symbols)
eth_five_ukf, eth_five_high_ukf, eth_five_low_ukf  = ukf_model.create_ukf_models("5T",  eth_symbols)

# ── GitHub Actions cron migration: persist/restore UKF filter state ──────────
# Each model above is created fresh (a full retrain against history, via
# create_ukf_models()) at every import — fine for a continuously-running
# process (it only happens once, at startup), but every one of these
# filters is then updated INCREMENTALLY, one bar at a time (step_ukf()'s
# .update()/.predict() calls), for as long as that one process stays
# alive, and only fully retrained again once a day (retrain_daily_ukfs()).
#
# A cron-triggered process (e.g. GitHub Actions) restarts from scratch on
# every single invocation. Without this, every tick would silently throw
# away all of that incremental adaptation and start the filters cold —
# which is a real change to the trading engine's behavior, not just a
# hosting swap. So: restore each filter's internal state (.x, .P) from
# the shared state store right after creation above (falling back to the
# freshly-trained values on a genuine first-ever run, when nothing has
# been persisted yet), and persist it again at the end of every task tick
# below and after every retrain_daily_ukfs() call.
#
# Per the standing "validate strategy-affecting changes via walkforward
# before trusting them live" rule: this restart-safe behavior has NOT yet
# been confirmed to reproduce the original continuous-process behavior —
# don't trust PYQUANT_MODE=paper/live output until a walkforward run (or
# an equivalent dedicated check) confirms restoring .x/.P this way tracks
# the same values a never-restarted process would have held.
_UKF_STATE_KEY = "pyq_p4_ukf_filter_state"

# Names only, looked up via globals() at call time rather than a dict of
# object references captured once — retrain_daily_ukfs() REBINDS the six
# 1H names (btc_ukf/btc_high_ukf/btc_low_ukf/eth_ukf/eth_high_ukf/
# eth_low_ukf) to brand-new objects each time it runs; a dict built once
# at import would keep pointing at the stale pre-retrain objects forever.
_UKF_NAMES = [
    "btc_ukf", "btc_high_ukf", "btc_low_ukf",
    "btc_fift_ukf", "btc_fift_high_ukf", "btc_fift_low_ukf",
    "btc_five_ukf", "btc_five_high_ukf", "btc_five_low_ukf",
    "eth_ukf", "eth_high_ukf", "eth_low_ukf",
    "eth_fift_ukf", "eth_fift_high_ukf", "eth_fift_low_ukf",
    "eth_five_ukf", "eth_five_high_ukf", "eth_five_low_ukf",
]


def _restore_ukf_state():
    """Best-effort: overwrite each freshly-created filter's .x/.P with the
    last-persisted values, if any. A miss (first-ever run, or a read
    error) just leaves the freshly-trained models from above in place."""
    saved = state_store.get_json(_UKF_STATE_KEY)
    if not saved:
        logger.info("No persisted UKF state found — using freshly-trained models (expected on first run).")
        return
    restored = 0
    for name in _UKF_NAMES:
        ukf = globals().get(name)
        entry = saved.get(name)
        if not entry or ukf is None:
            continue
        try:
            ukf.x = np.array(entry["x"], dtype=float)
            ukf.P = np.array(entry["P"], dtype=float)
            restored += 1
        except Exception as e:
            logger.error(f"UKF state restore failed for {name}: {e}")
    logger.info(f"Restored persisted UKF state for {restored}/{len(_UKF_NAMES)} models.")


def _persist_ukf_state():
    """Best-effort: snapshot every model's CURRENT .x/.P (via globals(),
    for the same reason _restore_ukf_state() looks up by name rather than
    a captured dict — retrain_daily_ukfs() rebinds six of these names).
    Called at the end of every task tick so a cron-restarted process can
    resume where the last invocation left off. A failure here never
    affects trading logic — it only means the next invocation starts from
    slightly staler state."""
    try:
        payload = {}
        for name in _UKF_NAMES:
            ukf = globals().get(name)
            if ukf is None:
                continue
            payload[name] = {"x": np.asarray(ukf.x).tolist(), "P": np.asarray(ukf.P).tolist()}
        state_store.set_json(_UKF_STATE_KEY, payload)
    except Exception as e:
        logger.error(f"UKF state persist failed (non-fatal): {e}")


_restore_ukf_state()

# ── Signal Generator & PositionGuard ──────────────────────────────────────────
signal_generator = SignalGenerator()

guard = PositionGuard(
    binance_client=binance_client,
    telegram_token=TELEGRAM_TOKEN,
    chat_id=TELEGRAM_CHAT_ID,
)
guard.start()

# ── UKF Mapping ───────────────────────────────────────────────────────────────
symbols_list = ["ETH/USD", "BTC/USD"]

ukf_mapping = {
    "ETH/USD": {"ukf": eth_ukf, "high_ukf": eth_high_ukf, "low_ukf": eth_low_ukf},
    "BTC/USD": {"ukf": btc_ukf, "high_ukf": btc_high_ukf, "low_ukf": btc_low_ukf},
}

# ── WATCHDOG TRACKER ENGINE ───────────────────────────────────────────────────

class WatchdogTracker:
    # Cron migration: this used to rely purely on `filename` for
    # persistence, which is correct for the VPS path (a real, persistent
    # disk) but not for GitHub Actions, where `actions/checkout` gives
    # every invocation a brand-new, empty working directory — a local
    # file written by one tick is simply gone by the next. The shared
    # state store (same DB pyquant_state.py already uses everywhere else)
    # is now the primary store; `filename` is kept as a best-effort local
    # mirror only, exactly like optimal_params.json's own local-file
    # mirror elsewhere in this project.
    _STATE_KEY = "pyq_p4_watchdog_trades"

    def __init__(self, filename="watchdog_trades.json"):
        self.filename = filename
        self.lock = threading.Lock()
        self.trades = self.load_trades()

    def load_trades(self) -> dict:
        remote = state_store.get_json(self._STATE_KEY)
        if isinstance(remote, dict):
            return remote
        if os.path.exists(self.filename):
            try:
                with open(self.filename, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_trades(self):
        try:
            state_store.set_json(self._STATE_KEY, self.trades)
        except Exception as e:
            logger.error(f"WatchdogTracker state-store save error (non-fatal): {e}")
        try:
            with open(self.filename, "w") as f:
                json.dump(self.trades, f, indent=4)
        except Exception as e:
            logger.error(f"WatchdogTracker local-file mirror save error (non-fatal): {e}")

    def register_signal(self, signal: dict, is_test_mode: bool = False, is_unfunded: bool = False):
        if not signal or "symbol" not in signal:
            return
        with self.lock:
            trade_id = f"{signal['symbol']}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
            self.trades[trade_id] = {
                "trade_id": trade_id,
                "symbol": signal["symbol"],
                "order_side": signal["order_side"],
                "order_type": signal["order_type"],
                "entry_price": float(signal["entry_price"]),
                "tp_price": float(signal["tp_price"]),
                "sl_price": float(signal["sl_price"]),
                "current_bal": float(signal.get("current_bal", 3.0)),
                "status": "PENDING",
                "registered_ts": datetime.now(timezone.utc).isoformat(),
                "fill_ts": None,
                "fill_price": None,
                "exit_ts": None,
                "exit_price": None,
                "exit_leg": None,
                "pnl": 0.0,
                "is_test_mode": is_test_mode,
                "is_unfunded": is_unfunded,
            }
            self.save_trades()
            logger.info(f"🐶 [WATCHDOG REGISTERED] {trade_id} | {signal['symbol']} {signal['order_side']} {signal['order_type']} @ ${signal['entry_price']:,.2f}")

    def process_minute_bar(self, symbol: str, bar_open: float, bar_high: float, bar_low: float, bar_close: float):
        if not WATCHDOG_MODE or not self.trades:
            return

        with self.lock:
            updated = False
            now_iso = datetime.now(timezone.utc).isoformat()

            for trade_id, t in list(self.trades.items()):
                if t["symbol"] != symbol or t["status"] == "CLOSED":
                    continue

                side  = t["order_side"]
                entry = t["entry_price"]
                tp    = t["tp_price"]
                sl    = t["sl_price"]

                # Step 1: Check PENDING signals for fill
                if t["status"] == "PENDING":
                    filled = False
                    fill_p = entry

                    if t["order_type"] == "Market":
                        filled = True
                        fill_p = bar_close
                    elif side == "Buy":
                        if bar_low <= entry:
                            filled = True
                            fill_p = entry
                    elif side == "Sell":
                        if bar_high >= entry:
                            filled = True
                            fill_p = entry

                    if filled:
                        t["status"]     = "FILLED"
                        t["fill_ts"]    = now_iso
                        t["fill_price"] = float(fill_p)
                        updated = True
                        logger.info(f"🐶 [WATCHDOG FILLED] {trade_id} | {symbol} {side} @ ${fill_p:,.2f}")

                # Step 2: Check active FILLED signals for TP/SL exit
                if t["status"] == "FILLED":
                    fp = t["fill_price"]
                    exit_leg  = None
                    exit_price = None

                    if side == "Buy":
                        h_sl = bar_low <= sl
                        h_tp = bar_high >= tp
                        if h_sl and h_tp:
                            exit_leg = "SL"; exit_price = sl
                        elif h_sl:
                            exit_leg = "SL"; exit_price = sl
                        elif h_tp:
                            exit_leg = "TP"; exit_price = tp
                    elif side == "Sell":
                        h_sl = bar_high >= sl
                        h_tp = bar_low <= tp
                        if h_sl and h_tp:
                            exit_leg = "SL"; exit_price = sl
                        elif h_sl:
                            exit_leg = "SL"; exit_price = sl
                        elif h_tp:
                            exit_leg = "TP"; exit_price = tp

                    if exit_leg:
                        bal = t["current_bal"]
                        qty = (bal * LEVERAGE) / fp if fp > 0 else 0
                        pnl = (exit_price - fp) * qty if side == "Buy" else (fp - exit_price) * qty
                        
                        t["status"]     = "CLOSED"
                        t["exit_ts"]    = now_iso
                        t["exit_price"] = float(exit_price)
                        t["exit_leg"]   = exit_leg
                        t["pnl"]        = float(pnl)
                        updated = True

                        logger.info(
                            f"🐶 [WATCHDOG EXIT] {trade_id} | {symbol} {side} closed via {exit_leg} "
                            f"@ ${exit_price:,.2f} | Virtual PnL: ${pnl:+.4f}"
                        )

            if updated:
                self.save_trades()


watchdog = WatchdogTracker()

# ── Module-level State ────────────────────────────────────────────────────────
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
    "entry_balance": None,
    "entry_capital": None,  # Phase 0: actual capital allocated to this trade
    "expected_max_loss": None,
}
eth_tracker = {
    "sig_gened": False, "ordered": False, "order_filled": False,
    "in_trade": False, "open_trade": False, "last_event_timestamp": None,
    "tp_hit": False, "sl_hit": False,
    "entry_balance": None,
    "entry_capital": None,  # Phase 0: actual capital allocated to this trade
    "expected_max_loss": None,
}

# ── GitHub Actions cron migration: persist/restore the tracker/flag state ──
# Same rationale as the UKF persistence above, for a different kind of
# state: btc_tracker/eth_tracker and the scalar flags below (double-order
# prevention, which symbol currently has an open position, etc.) are
# plain in-memory globals today, correct only because one continuous
# process keeps them alive between ticks. A cron-restarted process needs
# them restored from the shared state store instead.
_RUNTIME_FLAGS_KEY = "pyq_p4_runtime_flags"
_RUNTIME_SCALAR_NAMES = [
    "ordered", "double_order", "order_changed", "position_closed",
    "wallet_balance", "btc_balance", "eth_balance",
    "btc_ordered_signal", "eth_ordered_signal",
    "btc_position", "eth_position", "bo_declared", "eo_declared",
]


def _restore_runtime_flags():
    """Best-effort: overwrite the freshly-initialized defaults above with
    the last-persisted values, if any. Mirrors _restore_ukf_state()'s
    fallback behavior — a miss just leaves today's module-level defaults
    (all cleared/False/None) in place, which is exactly right on a
    genuine first-ever run."""
    saved = state_store.get_json(_RUNTIME_FLAGS_KEY)
    if not saved:
        logger.info("No persisted runtime-flag state found — using fresh defaults (expected on first run).")
        return
    g = globals()
    try:
        if isinstance(saved.get("btc_tracker"), dict):
            g["btc_tracker"].update(saved["btc_tracker"])
        if isinstance(saved.get("eth_tracker"), dict):
            g["eth_tracker"].update(saved["eth_tracker"])
        for name in _RUNTIME_SCALAR_NAMES:
            if name in saved:
                g[name] = saved[name]
        logger.info("Restored persisted runtime-flag state (tracker dicts + scalar flags).")
    except Exception as e:
        logger.error(f"Runtime-flag state restore failed: {e}")


def _persist_runtime_flags():
    """Best-effort: snapshot btc_tracker/eth_tracker + the scalar flags.
    Called at the same points as _persist_ukf_state() — see that
    function's docstring for why this has to be a snapshot-every-tick
    operation rather than a one-time save."""
    try:
        g = globals()
        payload = {"btc_tracker": dict(g["btc_tracker"]), "eth_tracker": dict(g["eth_tracker"])}
        for name in _RUNTIME_SCALAR_NAMES:
            payload[name] = g.get(name)
        state_store.set_json(_RUNTIME_FLAGS_KEY, payload)
    except Exception as e:
        logger.error(f"Runtime-flag state persist failed (non-fatal): {e}")


_restore_runtime_flags()

LAST_RESORT_MULTIPLIER = 1.5
HARD_DOLLAR_SL_FLOOR    = -0.20

LAST_RUN_FILE   = "last_execution_timestamp.txt"   # unused as of Phase 1 — kept only so an
MINUTE_RUN_FILE = "minute_execution_timestamp.txt"  # old deployment's leftover file is harmless


def retrain_daily_ukfs():
    """Re-anchors 1H UKFs on trailing 30 days of data once daily (matches walkforward.py)."""
    global btc_ukf, btc_high_ukf, btc_low_ukf
    global eth_ukf, eth_high_ukf, eth_low_ukf
    logger.info("[UKF Retrain] Re-anchoring 1H UKF models on trailing 30-day window…")

    for symbol_alpaca in ("BTC/USD", "ETH/USD"):
        try:
            hours_needed = UKF_TRAIN_DAYS * 24
            start_time = datetime.now(timezone.utc) - timedelta(hours=hours_needed + 5)
            request_params = CryptoBarsRequest(
                symbol_or_symbols=[symbol_alpaca],
                timeframe=TimeFrame.Hour,
                start=start_time,
            )
            bars = crypto_client.get_crypto_bars(request_params).df
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(symbol_alpaca, level="symbol")
            
            bars = bars.sort_index().tail(hours_needed).reset_index(drop=True)
            symb_short = "BTC" if "BTC" in symbol_alpaca else "ETH"
            
            ukf_c, ukf_h, ukf_l = build_ukfs(bars, symb_short)
            if symb_short == "BTC":
                btc_ukf, btc_high_ukf, btc_low_ukf = ukf_c, ukf_h, ukf_l
                ukf_mapping["BTC/USD"] = {"ukf": btc_ukf, "high_ukf": btc_high_ukf, "low_ukf": btc_low_ukf}
            else:
                eth_ukf, eth_high_ukf, eth_low_ukf = ukf_c, ukf_h, ukf_l
                ukf_mapping["ETH/USD"] = {"ukf": eth_ukf, "high_ukf": eth_high_ukf, "low_ukf": eth_low_ukf}

            logger.info(f"[UKF Retrain] Successfully re-anchored 1H UKF for {symb_short} on {len(bars)} bars.")
        except Exception as e:
            logger.error(f"[UKF Retrain] Error re-anchoring 1H UKF for {symbol_alpaca}: {e}")

    # Cron migration: this just rebound six of the global UKF names to
    # brand-new objects — persist immediately so the new post-retrain
    # state (not the pre-retrain snapshot from the top of this same
    # invocation) is what the next cron tick restores.
    _persist_ukf_state()
    _persist_runtime_flags()


# Phase 1: run guards now live in pyquant_state (SQLite locally, Postgres
# in production via DATABASE_URL) instead of local timestamp files.
# try_claim_run() is atomic and safe against two invocations racing each
# other or a QStash retry of the same delivery — the file-mtime comparison
# this replaced was neither (and wouldn't survive a stateless Vercel
# invocation at all). hourly_task()/one_minute_task() below now call
# try_claim_run() directly instead of has_function_run_this_hour() +
# update_last_run_timestamp(); those two helper functions are UNUSED and
# kept only as a reference for anyone diffing against the previous
# deployment.

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


def get_live_atr(symbol_alpaca: str) -> float:
    try:
        df = fetch_recent_bars(symbol_alpaca, hours=ATR_LOOKBACK_HOURS)
        if df is None or len(df) < 2:
            logger.warning(f"get_live_atr: insufficient bars for {symbol_alpaca} — falling back to fixed SL offset.")
            return 0.0
        return compute_atr(df, period=ATR_PERIOD)
    except Exception as e:
        logger.error(f"get_live_atr ({symbol_alpaca}): {e}")
        return 0.0


def simulate_signal_entry(signal: dict, trade_val: float):
    """SIGNAL_ONLY_MODE counterpart to signal_executor() below — same
    calling convention (hourly_task() calls this instead of
    signal_executor() when SIGNAL_ONLY_MODE is on), but makes NO exchange
    calls whatsoever: no futures_change_leverage(), no
    futures_exchange_info() lot-size lookup, no futures_create_order().

    It still does the one thing the user actually asked for: logs the
    signal to pyquant_signals (mode="simulated", so it's visibly distinct
    from a real live/paper entry on the UI's Signals page) and returns an
    ordered_signal dict with the exact keys one_minute_task()'s legacy
    tracker reads (order_type, entry_price, order_side, tp_price,
    sl_price, current_price, current_bal) — generate_signal() already
    produces all of these, so this function reuses `signal` itself rather
    than reshaping it. That tracker then runs its simulated TP/SL check
    against real incoming market bars exactly as it would for a real
    trade, which is the "just our simulated check if TP/SL hit" the user
    asked for — it's the same code path, just never preceded by a real
    order.

    Returns (signal, ordered) to match signal_executor()'s
    (signal, response, ordered) shape closely enough for the call sites
    below — ordered is True iff the signal had a usable entry_price.
    """
    global ordered

    if not signal or signal.get("entry_price") is None:
        logger.warning("simulate_signal_entry: invalid signal – nothing logged.")
        ordered = False
        return signal, ordered

    symbol      = signal["symbol"]
    order_side  = signal["order_side"].upper()
    entry_price = float(signal["entry_price"])

    # Approximate qty for display/logging only. Real orders round this to
    # the exchange's LOT_SIZE step (see signal_executor()) — that lookup
    # itself requires an exchange call this function must not make, and
    # the figure is never used to place an order here, so an unrounded
    # approximation is sufficient.
    try:
        qty = (trade_val * LEVERAGE) / entry_price if entry_price else 0.0
    except Exception:
        qty = 0.0
    signal["qty"] = qty

    logger.info(f"[SIGNAL-ONLY] {symbol} {order_side} {signal['order_type']} @ {entry_price} "
                f"— simulated entry, no exchange call made.")

    try:
        signal["state_signal_id"] = state_store.log_signal(
            symbol=symbol,
            side=order_side,
            entry_price=entry_price,
            sl_price=signal.get("sl_price"),
            tp_price=signal.get("tp_price"),
            qty=qty,
            entry_capital=trade_val,
            expected_max_loss=signal.get("expected_max_loss"),
            mode="simulated",
        )
    except Exception as e:
        logger.warning(f"simulate_signal_entry: log_signal failed (non-fatal): {e}")

    signals.append(signal)
    ordered = True
    return signal, ordered


def signal_executor(signal: dict, trade_val: float):
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

    # Phase 1: a deterministic client order id makes this entry order safe
    # to retry. Without one, Binance auto-generates a fresh id per call, so
    # a retry after a lost response (the request succeeded server-side,
    # but we never saw the reply) could open a SECOND real position —
    # the single most dangerous version of the duplicate-order risk
    # pyquant_resilience.py's module docstring describes, since this is
    # the entry itself, not a bracket SL/TP. The bucket is the current
    # hour because signals are generated once per hourly_task() run; two
    # calls for the SAME signal within the same hour are necessarily a
    # retry, never a legitimate second trade.
    client_order_id = f"pyq-{symbol}-{order_side}-{state_store.current_hour_bucket()}"[:36]

    order_params: dict = {
        "symbol": symbol, "side": order_side,
        "type": order_type, "quantity": quantity_str,
        "newClientOrderId": client_order_id,
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

        def _place():
            return binance_client.futures_create_order(**order_params)

        try:
            response = resilience.retry_api_call(_place, description=f"signal_executor entry ({symbol})")
        except Exception as e:
            if resilience.is_duplicate_order_error(e):
                # The order was actually placed on a prior attempt whose
                # response we never saw — look it up by the client id
                # instead of placing a second one.
                logger.warning(
                    f"SignalExecutor: {symbol} entry rejected as duplicate of "
                    f"clientOrderId={client_order_id} — looking up the original instead of retrying."
                )
                response = binance_client.futures_get_order(symbol=symbol, origClientOrderId=client_order_id)
            else:
                raise

        logger.info(f"SignalExecutor: order placed – ID={response['orderId']}")
        signal["orderId"] = response["orderId"]
        signals.append(signal)
        ordered = True

        # Phase 2: log this entry for the UI's Signals/Live Monitor screens.
        # Purely additive — never affects the trading decision above, and
        # a logging failure (state_store already fails closed internally)
        # must never take down the entry path.
        try:
            signal["state_signal_id"] = state_store.log_signal(
                symbol=symbol,
                side=order_side,
                entry_price=entry_price,
                sl_price=signal.get("sl_price"),
                tp_price=signal.get("tp_price"),
                qty=quantity,
                entry_capital=trade_val,
                expected_max_loss=signal.get("expected_max_loss"),
                mode=PYQUANT_MODE,
            )
        except Exception as e:
            logger.warning(f"SignalExecutor: log_signal failed (non-fatal): {e}")

        return signal, response, ordered
    except Exception as e:
        logger.error(f"SignalExecutor: order placement failed for {symbol}: {e}")
        ordered = False
        return signal, response, ordered


def _log_signal_close(sig: Optional[dict], status: str, outcome_price: float, realized_pnl: Optional[float] = None) -> None:
    """Best-effort Phase 2 close-out of a pyquant_signals row. No-op if the
    signal was never logged (sig is None, or had no state_signal_id —
    e.g. it predates this Phase 2 change, or logging failed at entry)."""
    if not sig:
        return
    sid = sig.get("state_signal_id")
    if not sid:
        return
    try:
        state_store.update_signal_outcome(sid, status, outcome_price=outcome_price, realized_pnl=realized_pnl)
    except Exception as e:
        logger.warning(f"update_signal_outcome failed (non-fatal): {e}")


def price_tracker(signal, pred, high_pred, low_pred, price, the_symbol):
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


def position_manager(symbol: str, data):
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


def last_resort_stop_loss_check(symbol: str, tracker: dict) -> bool:
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


def hourly_task():
    if not state_store.try_claim_run("hourly_task", state_store.current_hour_bucket()):
        print(f"Hourly task already ran (or is running) this hour. Skipping.")
        return

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

    # Phase 2: UI "OFF" control (pyquant_state.set_trading_enabled(False)).
    # Checked fresh every cycle, so a toggle from the UI takes effect on the
    # NEXT hourly_task() run without a process restart. Signal generation
    # and logging still run as normal when disabled — only NEW entry order
    # placement is skipped; PositionGuard keeps protecting any already-open
    # position regardless of this flag (see pyquant_state.py's docstring).
    trading_enabled = state_store.is_trading_enabled()
    if not trading_enabled:
        logger.warning("hourly_task: trading is OFF (set via UI) — signals will be generated but not executed.")

    now = datetime.now()
    print(f"[{now:%Y-%m-%d %H:%M:%S}] Running Hourly Task.")

    # Balance
    try:
        wallet_balance = None
        while wallet_balance is None:
            try:
                usdt_equity, btc_equity, eth_equity = get_equities()
                wallet_balance = float(usdt_equity) if usdt_equity > 0 else 3.0
                btc_balance    = float(btc_equity)
                eth_balance    = float(eth_equity)
                logger.info(f"Balance – USDT:{wallet_balance}  BTC:{btc_balance}  ETH:{eth_balance}")
            except Exception as e:
                logger.error(f"Error getting balance: {e}")
                time.sleep(2)

        if not TEST_MODE:
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

    # Position backstop checks
    if not TEST_MODE:
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
                                             "entry_balance": None, "entry_capital": None, "expected_max_loss": None})
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
                                             "entry_balance": None, "entry_capital": None, "expected_max_loss": None})
                        in_trade = eth_tracker["in_trade"] = False
            else:
                in_trade = eth_tracker["in_trade"] = False

        except Exception as e:
            logger.error(f"Position check error: {e}")

    position_closed = False
    atr_mult = signal_generator.load_params().get("atr_mult", 1.8)

    # Signal generation
    logger.info("Starting Hourly Signal Services")
    try:
        for symbol in symbols_list:
            trade_symbol = "BTCUSDT" if symbol in ("BTC/USD", "BTCUSDT") else "ETHUSDT"
            ukf_data = ukf_mapping.get(symbol)

            if not TEST_MODE:
                already_open = check_open_position(symbol=trade_symbol)["is_open_position"]
                if already_open:
                    logger.info(f"{symbol} already has an open position — skipping new signal generation.")
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

            price = float(ddata["close"])

            try:
                position_manager(symbol, ddata)
                if symbol in ("ETH/USD", "ETHUSDT") and not TEST_MODE:
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

                if WATCHDOG_MODE:
                    watchdog.register_signal(my_signal, is_test_mode=TEST_MODE, is_unfunded=(wallet_balance <= 0))
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
            trade_value = float(wallet_balance) / 2.0  # Consolidated 50/50 capital split

            # Phase 0 fix: expected_max_loss was computed inside
            # generate_signal() against the full wallet_balance, before
            # this 50/50 split was known — recalculate it now against the
            # capital actually allocated to each trade, so the last-resort
            # stop (LAST_RESORT_MULTIPLIER * expected_max_loss) is
            # calibrated correctly.
            eth_signal["expected_max_loss"] = signal_math.expected_max_loss(
                eth_signal["entry_price"], eth_signal["sl_price"], trade_value, LEVERAGE)
            btc_signal["expected_max_loss"] = signal_math.expected_max_loss(
                btc_signal["entry_price"], btc_signal["sl_price"], trade_value, LEVERAGE)

            if SIGNAL_ONLY_MODE:
                # See SIGNAL_ONLY_MODE's definition above: generate + log +
                # simulated-track, zero exchange calls, takes priority over
                # TEST_MODE/trading_enabled below.
                try:
                    eth_ordered_signal, eth_ordered = simulate_signal_entry(eth_signal, trade_value)
                except Exception as e:
                    logger.error(f"ETH signal-only simulate error: {e}")
                    eth_ordered_signal, eth_ordered = None, False

                try:
                    btc_ordered_signal, btc_ordered = simulate_signal_entry(btc_signal, trade_value)
                except Exception as e:
                    logger.error(f"BTC signal-only simulate error: {e}")
                    btc_ordered_signal, btc_ordered = None, False

                double_order = True
                signal_generator.sig_gened = True
                ordered = True
                eth_tracker.update({"sig_gened": True, "ordered": eth_ordered, "last_event_timestamp": now,
                                     "entry_balance": wallet_balance, "entry_capital": trade_value,
                                     "expected_max_loss": eth_signal.get("expected_max_loss")})
                btc_tracker.update({"sig_gened": True, "ordered": btc_ordered, "last_event_timestamp": now,
                                     "entry_balance": wallet_balance, "entry_capital": trade_value,
                                     "expected_max_loss": btc_signal.get("expected_max_loss")})
            elif not TEST_MODE and trading_enabled:
                try:
                    eth_ordered_signal, eth_response, eth_ordered = signal_executor(eth_signal, trade_value)
                    if eth_response:
                        guard.start_guard_for_order(eth_signal, eth_response["orderId"])
                except Exception as e:
                    logger.error(f"ETH order execution error: {e}")

                try:
                    btc_ordered_signal, btc_response, btc_ordered = signal_executor(btc_signal, trade_value)
                    if btc_response:
                        guard.start_guard_for_order(btc_signal, btc_response["orderId"])
                except Exception as e:
                    logger.error(f"BTC order execution error: {e}")

                double_order = True
                signal_generator.sig_gened = True
                ordered = True
                eth_tracker.update({"sig_gened": True, "ordered": eth_ordered, "last_event_timestamp": now,
                                     "entry_balance": wallet_balance, "entry_capital": trade_value,
                                     "expected_max_loss": eth_signal.get("expected_max_loss")})
                btc_tracker.update({"sig_gened": True, "ordered": btc_ordered, "last_event_timestamp": now,
                                     "entry_balance": wallet_balance, "entry_capital": trade_value,
                                     "expected_max_loss": btc_signal.get("expected_max_loss")})
            elif not trading_enabled:
                logger.info("[TRADING OFF] Both signals generated; order execution skipped (UI switch).")
            else:
                logger.info("[TEST MODE] Both signals generated; live order execution bypassed.")

        elif btc_tracker["sig_gened"] and not eth_tracker["sig_gened"]:
            trade_value = float(wallet_balance) * 0.8
            # Phase 0 fix: recalculate against the actually-allocated 80%,
            # not the full wallet_balance generate_signal() saw.
            btc_signal["expected_max_loss"] = signal_math.expected_max_loss(
                btc_signal["entry_price"], btc_signal["sl_price"], trade_value, LEVERAGE)
            if SIGNAL_ONLY_MODE:
                try:
                    btc_ordered_signal, btc_ordered = simulate_signal_entry(btc_signal, trade_value)
                except Exception as e:
                    logger.error(f"BTC signal-only simulate error: {e}")
                    btc_ordered_signal, btc_ordered = None, False

                eth_ordered_signal = None
                signal_generator.sig_gened = True
                ordered      = True
                double_order = False
                btc_tracker.update({"ordered": btc_ordered, "last_event_timestamp": now,
                                     "entry_balance": wallet_balance, "entry_capital": trade_value,
                                     "expected_max_loss": btc_signal.get("expected_max_loss")})
                eth_tracker["sig_gened"] = False
            elif not TEST_MODE and trading_enabled:
                btc_ordered_signal, btc_response, btc_ordered = signal_executor(btc_signal, trade_value)
                try:
                    if btc_response:
                        guard.start_guard_for_order(btc_signal, btc_response["orderId"])
                except Exception as e:
                    logger.error(f"BTC guard start error: {e}")

                eth_ordered_signal = None
                signal_generator.sig_gened = True
                ordered      = True
                double_order = False
                btc_tracker.update({"ordered": btc_ordered, "last_event_timestamp": now,
                                     "entry_balance": wallet_balance, "entry_capital": trade_value,
                                     "expected_max_loss": btc_signal.get("expected_max_loss")})
                eth_tracker["sig_gened"] = False
            elif not trading_enabled:
                logger.info("[TRADING OFF] BTC signal generated; order execution skipped (UI switch).")
            else:
                logger.info("[TEST MODE] BTC signal generated; live order execution bypassed.")

        elif eth_tracker["sig_gened"] and not btc_tracker["sig_gened"]:
            trade_value = float(wallet_balance) * 0.8
            # Phase 0 fix: recalculate against the actually-allocated 80%,
            # not the full wallet_balance generate_signal() saw.
            eth_signal["expected_max_loss"] = signal_math.expected_max_loss(
                eth_signal["entry_price"], eth_signal["sl_price"], trade_value, LEVERAGE)
            if SIGNAL_ONLY_MODE:
                try:
                    eth_ordered_signal, eth_ordered = simulate_signal_entry(eth_signal, trade_value)
                except Exception as e:
                    logger.error(f"ETH signal-only simulate error: {e}")
                    eth_ordered_signal, eth_ordered = None, False

                btc_ordered_signal = None
                signal_generator.sig_gened = True
                ordered      = True
                double_order = False
                eth_tracker.update({"sig_gened": True, "ordered": eth_ordered,
                                     "last_event_timestamp": now,
                                     "entry_balance": wallet_balance, "entry_capital": trade_value,
                                     "expected_max_loss": eth_signal.get("expected_max_loss")})
                btc_tracker["sig_gened"] = False
            elif not TEST_MODE and trading_enabled:
                eth_ordered_signal, eth_response, eth_ordered = signal_executor(eth_signal, trade_value)
                try:
                    if eth_response:
                        guard.start_guard_for_order(eth_signal, eth_response["orderId"])
                except Exception as e:
                    logger.error(f"ETH guard start error: {e}")

                btc_ordered_signal = None
                signal_generator.sig_gened = True
                ordered      = True
                double_order = False
                eth_tracker.update({"sig_gened": True, "ordered": eth_ordered,
                                     "last_event_timestamp": now,
                                     "entry_balance": wallet_balance, "entry_capital": trade_value,
                                     "expected_max_loss": eth_signal.get("expected_max_loss")})
                btc_tracker["sig_gened"] = False
            elif not trading_enabled:
                logger.info("[TRADING OFF] ETH signal generated; order execution skipped (UI switch).")
            else:
                logger.info("[TEST MODE] ETH signal generated; live order execution bypassed.")

        else:
            print("No signals from either coin. No orders placed.")
            logger.info("No signal from either coin this hour.")
            btc_ordered_signal = eth_ordered_signal = None
            signal_generator.sig_gened = False
            btc_tracker.update({"sig_gened": False, "ordered": False})
            eth_tracker.update({"sig_gened": False, "ordered": False})

    except Exception as e:
        logger.error(f"Signal execution error: {e}\n{traceback.format_exc()}")

    _persist_ukf_state()      # cron migration — best-effort, see definition above
    _persist_runtime_flags()  # cron migration — best-effort, see definition above


def fifteen_minute_task():
    # Cron migration: fifteen_minute_task() previously had no run-guard of
    # its own — it relied entirely on p4_orchestra.py's schedule library
    # calling it at exactly :00/:15/:30/:45 within one continuous process.
    # A cron-triggered process calling it unconditionally on every 5-minute
    # tick needs this guard so it still only runs once per 15-minute
    # window, not 3x too often.
    if not state_store.try_claim_run("fifteen_minute_task", state_store.current_15min_bucket()):
        print("15-minute task already ran (or is running) this window. Skipping.")
        return

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

    _persist_ukf_state()      # cron migration — best-effort, see definition above
    _persist_runtime_flags()  # cron migration — best-effort, see definition above


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

    _persist_ukf_state()      # cron migration — best-effort, see definition above
    _persist_runtime_flags()  # cron migration — best-effort, see definition above


def one_minute_task():
    if not state_store.try_claim_run("one_minute_task", state_store.current_minute_bucket()):
        print(f"1-minute task already ran (or is running) this minute. Skipping.")
        return

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

            price = float(one_data["close"])
            llow  = float(one_data["low"])
            hhigh = float(one_data["high"])
            print(f"{i}: O:{one_data['open']}  H:{hhigh}  L:{llow}  C:{price}")

            # Watchdog Processing
            watchdog.process_minute_bar(
                symbol="BTCUSDT" if "BTC" in i else "ETHUSDT",
                bar_open=float(one_data["open"]),
                bar_high=hhigh,
                bar_low=llow,
                bar_close=price
            )

            # PositionGuard virtual TP/SL safeguard
            if guard and guard.monitored_orders:
                for order_id, order_info in list(guard.monitored_orders.items()):
                    if (order_info["symbol"] == i or order_info["symbol"].replace("/", "") == i.replace("/", "")):
                        sig = order_info.get("signal")
                        if not sig: continue
                        virtual_close = False; reason = ""
                        if sig["order_side"] == "Buy":
                            if hhigh >= sig["tp_price"]: virtual_close = True; reason = "Virtual TP (Buy)"
                            elif llow <= sig["sl_price"]: virtual_close = True; reason = "Virtual SL (Buy)"
                        elif sig["order_side"] == "Sell":
                            if llow <= sig["tp_price"]: virtual_close = True; reason = "Virtual TP (Sell)"
                            elif hhigh >= sig["sl_price"]: virtual_close = True; reason = "Virtual SL (Sell)"

                        if virtual_close:
                            logger.warning(f"🚨 {reason} HIT for {i}. Checking position…")
                            pos_check = check_open_position(symbol=order_info["symbol"])
                            if pos_check["is_open_position"]:
                                logger.warning("Position OPEN – executing emergency close.")
                                close_futures_position(pos_check)
                                guard.add_alert_to_queue(f"✅ {reason} executed via 1m safeguard for {i}")
                                _log_signal_close(
                                    sig, "closed_tp" if "TP" in reason else "closed_sl",
                                    outcome_price=sig.get("tp_price") if "TP" in reason else sig.get("sl_price"),
                                )
                                del guard.monitored_orders[order_id]
                                guard.save_to_json()
                                if order_info["symbol"] == "BTCUSDT":
                                    btc_tracker.update({"open_trade": False, "entry_balance": None, "entry_capital": None, "expected_max_loss": None})
                                elif order_info["symbol"] == "ETHUSDT":
                                    eth_tracker.update({"open_trade": False, "entry_balance": None, "entry_capital": None, "expected_max_loss": None})

            # Dynamic last-resort PnL-based stop loss — also skipped in
            # SIGNAL_ONLY_MODE: there is never a real position to check for
            # a signal-only "trade" (no order was ever placed for it), so
            # this would just be a needless Binance read call every tick.
            if not TEST_MODE and not SIGNAL_ONLY_MODE:
                if i in ("BTC/USD", "BTCUSDT") and btc_tracker["open_trade"]:
                    if last_resort_stop_loss_check("BTCUSDT", btc_tracker):
                        btc_tracker.update({"open_trade": False, "sl_hit": True, "entry_balance": None, "entry_capital": None, "expected_max_loss": None})

                if i in ("ETH/USD", "ETHUSDT") and eth_tracker["open_trade"]:
                    if last_resort_stop_loss_check("ETHUSDT", eth_tracker):
                        eth_tracker.update({"open_trade": False, "sl_hit": True, "entry_balance": None, "entry_capital": None, "expected_max_loss": None})

            # Legacy BTC manual tracker (Preserved)
            if i in ("BTC/USD", "BTCUSDT"):
                try:
                    bo_signal = btc_ordered_signal
                    if bo_signal is not None:
                        btc_position_status = check_open_position(symbol="BTCUSDT")
                        if not btc_tracker["order_filled"] and btc_tracker["sig_gened"]:
                            if bo_signal["order_type"] == "Market":
                                btc_tracker.update({"order_filled": True, "open_trade": True})
                                logger.info(f"{i} market order filled. Price:{price}")
                            if bo_signal["current_price"] > bo_signal["entry_price"] and price <= bo_signal["entry_price"]:
                                btc_tracker.update({"order_filled": True, "open_trade": True})
                                logger.info(f"{i} BUY limit filled. Price:{price}")
                            elif bo_signal["current_price"] < bo_signal["entry_price"] and price >= bo_signal["entry_price"]:
                                btc_tracker.update({"order_filled": True, "open_trade": True})
                                logger.info(f"{i} SELL limit filled. Price:{price}")

                        if btc_tracker["open_trade"] and not btc_tracker["tp_hit"] and not btc_tracker["sl_hit"]:
                            pnl = float(bo_signal["current_bal"]) - (wallet_balance or 0)
                            if bo_signal["order_side"] == "Buy":
                                if hhigh >= bo_signal["tp_price"]:
                                    btc_tracker.update({"tp_hit": True, "open_trade": False, "entry_balance": None, "entry_capital": None, "expected_max_loss": None})
                                    _log_signal_close(bo_signal, "closed_tp", outcome_price=price, realized_pnl=pnl)
                                    logger.info(f"{i} TP hit. Price:{price}  PNL:{pnl}")
                                elif llow <= bo_signal["sl_price"]:
                                    btc_tracker.update({"sl_hit": True, "open_trade": False, "entry_balance": None, "entry_capital": None, "expected_max_loss": None})
                                    _log_signal_close(bo_signal, "closed_sl", outcome_price=price, realized_pnl=pnl)
                                    logger.info(f"{i} SL hit. Price:{price}  PNL:{pnl}")
                            elif bo_signal["order_side"] == "Sell":
                                if llow <= bo_signal["tp_price"]:
                                    btc_tracker.update({"tp_hit": True, "open_trade": False, "entry_balance": None, "entry_capital": None, "expected_max_loss": None})
                                    _log_signal_close(bo_signal, "closed_tp", outcome_price=price, realized_pnl=pnl)
                                    logger.info(f"{i} TP hit. Price:{price}  PNL:{pnl}")
                                elif hhigh >= bo_signal["sl_price"]:
                                    btc_tracker.update({"sl_hit": True, "open_trade": False, "entry_balance": None, "entry_capital": None, "expected_max_loss": None})
                                    _log_signal_close(bo_signal, "closed_sl", outcome_price=price, realized_pnl=pnl)
                                    logger.info(f"{i} SL hit. Price:{price}  PNL:{pnl}")
                            else:
                                print("BTC order tracking: unknown order_side.")

                    elif bo_signal is None and btc_tracker["sig_gened"] and not bo_declared:
                        logger.info("BTC sig_gened=True but no ordered_signal – order likely failed.")
                        bo_declared = True

                except Exception as e:
                    print(f"BTC 1m tracker error: {e}")

            # Legacy ETH manual tracker (Preserved)
            if i in ("ETH/USD", "ETHUSDT"):
                try:
                    eo_signal = eth_ordered_signal
                    if eo_signal is not None:
                        eth_position_status = check_open_position(symbol="ETHUSDT")
                        if not eth_tracker["order_filled"] and eth_tracker["sig_gened"]:
                            if eo_signal["order_type"] == "Market":
                                eth_tracker.update({"order_filled": True, "open_trade": True})
                                logger.info(f"{i} market order filled. Price:{price}")
                            if eo_signal["current_price"] > eo_signal["entry_price"] and price <= eo_signal["entry_price"]:
                                eth_tracker.update({"order_filled": True, "open_trade": True})
                                logger.info(f"{i} BUY limit filled. Price:{price}")
                            elif eo_signal["current_price"] < eo_signal["entry_price"] and price >= eo_signal["entry_price"]:
                                eth_tracker.update({"order_filled": True, "open_trade": True})
                                logger.info(f"{i} SELL limit filled. Price:{price}")

                        if eth_tracker["open_trade"] and not eth_tracker["tp_hit"] and not eth_tracker["sl_hit"]:
                            pnl = float(eo_signal["current_bal"]) - (wallet_balance or 0)
                            if eo_signal["order_side"] == "Buy":
                                if hhigh >= eo_signal["tp_price"]:
                                    eth_tracker.update({"tp_hit": True, "open_trade": False, "entry_balance": None, "entry_capital": None, "expected_max_loss": None})
                                    _log_signal_close(eo_signal, "closed_tp", outcome_price=price, realized_pnl=pnl)
                                    logger.info(f"{i} TP hit. Price:{price}  PNL:{pnl}")
                                elif llow <= eo_signal["sl_price"]:
                                    eth_tracker.update({"sl_hit": True, "open_trade": False, "entry_balance": None, "entry_capital": None, "expected_max_loss": None})
                                    _log_signal_close(eo_signal, "closed_sl", outcome_price=price, realized_pnl=pnl)
                                    logger.info(f"{i} SL hit. Price:{price}  PNL:{pnl}")
                            elif eo_signal["order_side"] == "Sell":
                                if llow <= eo_signal["tp_price"]:
                                    eth_tracker.update({"tp_hit": True, "open_trade": False, "entry_balance": None, "entry_capital": None, "expected_max_loss": None})
                                    _log_signal_close(eo_signal, "closed_tp", outcome_price=price, realized_pnl=pnl)
                                    logger.info(f"{i} TP hit. Price:{price}  PNL:{pnl}")
                                elif hhigh >= eo_signal["sl_price"]:
                                    eth_tracker.update({"sl_hit": True, "open_trade": False, "entry_balance": None, "entry_capital": None, "expected_max_loss": None})
                                    _log_signal_close(eo_signal, "closed_sl", outcome_price=price, realized_pnl=pnl)
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

    _persist_ukf_state()      # cron migration — best-effort, see definition above
    _persist_runtime_flags()  # cron migration — best-effort, see definition above