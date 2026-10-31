"""
╔══════════════════════════════════════════════════════════════════════════════╗
║    PyQuant Walk-Forward Backtest — v4 Production Edition                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v4 CHANGES FROM v3                                                          ║
║  ──────────────────                                                          ║
║  FIX 1  Entry fill validation — a Limit entry is only accepted if its price  ║
║         actually falls within [bar_low, bar_high]. Previously every entry    ║
║         "filled" regardless of whether the market ever traded at that       ║
║         price, which silently inflated trade count and returns.             ║
║                                                                              ║
║  FIX 2  BTC and ETH trade independently. Previously `if in_b or in_e:        ║
║         continue` blocked EITHER symbol from entering whenever the OTHER     ║
║         had an open position — inconsistent with the live system's          ║
║         separately-tracked btc_tracker/eth_tracker. Each symbol now enters   ║
║         and exits on its own schedule.                                       ║
║                                                                              ║
║  FIX 3  paper_trade_day() is now wrapped in try/except at its call site.     ║
║         A bad bar/day no longer crashes the entire multi-day run — it is     ║
║         logged and skipped, matching the defensive pattern already used     ║
║         for UKF training and optimizer failures.                             ║
║                                                                              ║
║  FIX 4  ATR cold-start eliminated. The last ATR_PERIOD bars of the prior     ║
║         training window are carried forward purely for ATR computation, so   ║
║         the first bars of every test day have a full-length lookback        ║
║         instead of starting from near-zero history.                         ║
║                                                                              ║
║  FIX 5  Positions are NO LONGER force-closed at the end of each day. This    ║
║         matches the live system, which never liquidates a healthy position  ║
║         just because a calendar day ended — hourly_task and one_minute_task  ║
║         manage positions purely by TP/SL/reversal, irrespective of the      ║
║         clock. Open positions now carry across day boundaries and are only  ║
║         force-closed ONCE, after the entire walk-forward period ends, to    ║
║         produce a final realized return figure.                             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, math, time, warnings
from itertools import product
from typing import Optional

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

from filterpy.kalman import UnscentedKalmanFilter, MerweScaledSigmaPoints
from filterpy.common import Q_discrete_white_noise

import pyquant_signal_math as signal_math
import pyquant_state as state_store

warnings.filterwarnings('ignore')

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    OPTUNA_AVAILABLE = True
except ImportError:
    OPTUNA_AVAILABLE = False
    print("⚠  Optuna not installed → using grid search fallback.")
    print("   Install: pip install optuna")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

LEVERAGE              = signal_math.LEVERAGE_DEFAULT  # 10x — shared with live (Phase 0 unification)
INITIAL_CAPITAL       = 10.0
UKF_TRAIN_DAYS        = 30
GRID_SEARCH_DAYS      = 15
WALK_FORWARD_DAYS     = 15

# 2026-10-31: explicit date-range override for the Backtest page's
# "start/end date" picker, as an alternative to WALK_FORWARD_DAYS' plain
# day-count (which always means "the last N days ending at whatever the
# CSV's newest bar is"). Both None (the default) keeps the original
# day-count behavior untouched. When set, run_walk_forward() resolves
# them against the historical CSV's own 'timestamp' column (data_download.py's
# ~366-day trailing window — see its own docstring) rather than fetching
# new data, and reassigns WALK_FORWARD_DAYS/n_bars to match — no other
# code in this function needed to change. ISO date strings ("YYYY-MM-DD").
START_DATE            = None
END_DATE              = None

USE_BAYESIAN          = True
BAYESIAN_TRIALS       = 150
PARAM_SMOOTHING_ALPHA = 0.7

# Kaggle backtest integration: a caller that already has its OWN
# pyquant_runs row to update (the Kaggle-invoked path — see
# kaggle_backtest/run_backtest.py) sets this True before calling
# run_walk_forward(), so the function's own automatic record_run() call
# near the end (which always creates a brand-new row with no knowledge of
# any caller-tracked run id) is skipped — the caller does its own
# state_store.update_run_status(run_id, ...) instead, so the UI's queued
# row gets updated in place rather than ending up with an orphaned
# "queued" row plus an unrelated duplicate "completed" one. Deliberately
# a tiny, additive, logging-only guard, not a change to any strategy
# logic — consistent with this project's "testing deferred to
# integration" rule only restricting touches to the actual trading
# decisions, not to purely-additive Phase 2 instrumentation. Manual CLI
# usage (`python3 walkforward.py`) is completely unaffected — this stays
# False unless a caller explicitly sets it.
_SUPPRESS_AUTO_RECORD_RUN = False


def _ts_to_str(v) -> Optional[str]:
    if v is None:
        return None
    try:
        return v.isoformat()
    except AttributeError:
        return str(v)


def trades_to_records(all_trades: list, leverage: Optional[float] = None) -> list:
    """Normalizes this module's own internal per-trade dict shape (built
    throughout run_walk_forward()'s simulation loop — symbol/side/
    entry_price/qty/tp_price/sl_price/ts/exit_price/exit_ts/exit_leg/
    profit) into the plain, stable shape
    pyquant_state.record_backtest_trades() expects, so a future refactor
    of the simulation loop's own trade-dict keys doesn't silently break
    the persisted comparison data (PLAN.md's 2026-10-27 Decisions entry
    on why this table exists — the prerequisite for a real
    Backtest-vs-Live per-trade comparison). `leverage` defaults to this
    module's own LEVERAGE constant (whatever it was actually run with,
    including a workflow_dispatch/Kaggle override) so `pnl_pct` is
    leveraged the exact same way pyq_p4.py's live outcome_pct() calls
    are — directly comparable to a live signal's realized_pnl_pct."""
    lev = leverage if leverage is not None else LEVERAGE
    out = []
    for t in all_trades:
        side = t.get("side") or ""
        entry = t.get("entry_price")
        exit_price = t.get("exit_price")
        pnl_pct = None
        if entry and exit_price and side:
            try:
                pnl_pct = signal_math.outcome_pct(side, entry, exit_price, lev)
            except Exception:
                pnl_pct = None
        out.append({
            "symbol": t.get("symbol"),
            "side": side,
            "entry_price": entry,
            "exit_price": exit_price,
            "tp_price": t.get("tp_price"),
            "sl_price": t.get("sl_price"),
            "qty": t.get("qty"),
            "exit_leg": t.get("exit_leg"),
            "profit": t.get("profit"),
            "pnl_pct": pnl_pct,
            "entry_ts": _ts_to_str(t.get("ts")),
            "exit_ts": _ts_to_str(t.get("exit_ts")),
        })
    return out

USE_ATR_SL            = True    # ATR-adaptive SL is active
ATR_PERIOD            = 14
ATR_SL_RANGE          = (1.0, 3.0)
# SL floor/delta and the ATR multiplier default now come from the shared
# module (Phase 0 unification) — kept as module-level names here (rather
# than inlined at every call site) so a run's printed config and its
# metrics still show these values directly.
ATR_SL_MULTIPLIER     = signal_math.DEFAULT_ATR_MULTIPLIER
BTC_SL_FLOOR          = signal_math.SL_FLOOR["BTC"]
ETH_SL_FLOOR          = signal_math.SL_FLOOR["ETH"]

BTC_SL_DELTA          = signal_math.SL_FIXED_DELTA["BTC"]
ETH_SL_DELTA          = signal_math.SL_FIXED_DELTA["ETH"]

# Profit-target early-exit fractions (of margin-at-risk) — shared with live.
BTC_PROFIT_TARGET     = signal_math.PROFIT_TARGET_FRACTION["BTC"]
ETH_PROFIT_TARGET     = signal_math.PROFIT_TARGET_FRACTION["ETH"]

USE_PESSIMISTIC_EXITS = True
ENABLE_PLOTTING       = True

BTC_CSV = 'btc_back366.csv'
ETH_CSV = 'eth_back366.csv'

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — EXECUTION COST MODEL
# ══════════════════════════════════════════════════════════════════════════════

MAKER_FEE            = 0.0002
TAKER_FEE            = 0.0005
ENTRY_SLIPPAGE_PCT   = 0.0003
TP_EXIT_SLIPPAGE_PCT = 0.0004
SL_EXIT_SLIPPAGE_PCT = 0.0008
FUNDING_RATE_PER_8H  = 0.0001
MIN_NOTIONAL         = 5.0
BTC_LOT_STEP         = 0.001
ETH_LOT_STEP         = 0.001

STRICT_BTC_VALS  = [0.0025, 0.00275, 0.003, 0.0035, 0.004, 0.0045, 0.005]
STRICTS_BTC_VALS = [0.004,  0.0045,  0.005, 0.0055, 0.006, 0.007,  0.0075]
STRICT_ETH_VALS  = [0.0035, 0.004,   0.0045, 0.005, 0.0055, 0.006]
STRICTS_ETH_VALS = [0.0045, 0.005,   0.0055, 0.006, 0.0065, 0.007, 0.0075, 0.008]

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — ACCOUNTING & EXECUTION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def floor_to_lot(qty: float, step: float) -> float:
    return math.floor(qty / step) * step


def is_fillable(entry_price: float, bar_low: float, bar_high: float) -> bool:
    """
    FIX 1 — Validates that a Limit entry price actually traded within the bar.

    Market orders always pass trivially (entry = close, which is always
    within [low, high] by definition). Limit orders at a predicted price
    (low_pred for BUY, high_pred for SELL) must fall within the bar's actual
    traded range, or the order would never have filled in reality. Without
    this check, every Limit signal "fills" regardless of whether the market
    ever reached that price, which silently inflates both trade count and
    reported returns versus what would actually happen live.
    """
    return bar_low <= entry_price <= bar_high


def apply_entry(signal_price: float, side: str, capital: float,
                leverage: float, lot_step: float):
    """
    Simulates a realistic LIMIT order fill.
    qty = (capital × leverage) / filled_price — leverage is embedded in qty.
    PnL formula = qty × price_change — no additional leverage multiplier.
    """
    slip    = ENTRY_SLIPPAGE_PCT
    filled  = signal_price * (1 + slip) if side == 'Buy' else signal_price * (1 - slip)
    qty     = floor_to_lot((capital * leverage) / filled, lot_step)
    valid   = qty > 0 and (qty * filled) >= MIN_NOTIONAL
    return filled, qty, MAKER_FEE * qty * filled, valid


def apply_exit(trigger: float, leg: str, side: str, qty: float):
    slip   = SL_EXIT_SLIPPAGE_PCT if leg == 'SL' else TP_EXIT_SLIPPAGE_PCT
    filled = trigger * (1 - slip) if side == 'Buy' else trigger * (1 + slip)
    return filled, TAKER_FEE * qty * filled


def market_exit(price: float, qty: float):
    return price, TAKER_FEE * qty * price


def compute_profit(side: str, entry: float, exit_p: float, qty: float) -> float:
    # Delegates to the shared module (Phase 0 unification) — kept as a
    # local name since it's called throughout this file.
    return signal_math.compute_profit(side, entry, exit_p, qty)


def funding_cost(entry_ts, exit_ts, qty: float, entry_price: float) -> float:
    """
    Funding on full leveraged notional. Correctly handles multi-day holds
    now that positions can carry across day boundaries (FIX 5) — periods
    are computed from raw elapsed time regardless of how many calendar
    days that spans.
    """
    if entry_ts is None or exit_ts is None:
        return 0.0
    try:
        ed, xd = pd.to_datetime(entry_ts), pd.to_datetime(exit_ts)
        if pd.isna(ed) or pd.isna(xd):
            return 0.0
        periods = int((xd - ed).total_seconds() // (8 * 3600))
        return max(0, periods) * FUNDING_RATE_PER_8H * qty * entry_price
    except Exception:
        return 0.0

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — ATR CALCULATION AND SL PRICING
# ══════════════════════════════════════════════════════════════════════════════

def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """
    Average True Range using Wilder's method.
    Returns 0.0 if insufficient data — sl_price() then falls back to the
    fixed-offset method, so no trade is ever left without a SL.
    """
    if len(df) < 2:
        return 0.0
    h = df['high'].values; l = df['low'].values; c = df['close'].values
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]),
                               np.abs(l[1:] - c[:-1])))
    n = min(period, len(tr))
    return float(np.mean(tr[-n:])) if n > 0 else 0.0


def sl_price(entry: float, side: str, symbol: str,
             atr: float = 0.0, atr_mult: float = None) -> float:
    """
    ATR-adaptive: delta = max(atr_mult × ATR, fixed_floor).
    Fixed:        delta = $225 (BTC) / $16 (ETH).
    Delegates to the shared module (Phase 0 unification) — the USE_ATR_SL
    gate stays local since live has no equivalent toggle (it always
    attempts ATR-adaptive SL when ATR data is available).
    """
    effective_atr = atr if USE_ATR_SL else 0.0
    return entry - signal_math.sl_delta(symbol, effective_atr, atr_mult) if side == 'Buy' \
        else entry + signal_math.sl_delta(symbol, effective_atr, atr_mult)

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — UKF ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _stabilise_P(ukf, floor: float = 1e-6):
    P = (ukf.P + ukf.P.T) / 2
    for i in range(P.shape[0]):
        if P[i, i] < floor:
            P[i, i] = floor
    ukf.P = P


def _make_ukf(init_val, alpha, beta, kappa, P_init, Q_scale, R_init):
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


def _train_ukf(values, alpha, beta, kappa, P_init, Q_scale, R_init):
    ukf = _make_ukf(values[0], alpha, beta, kappa, P_init, Q_scale, R_init)
    for z in values:
        ukf.predict(); _stabilise_P(ukf)
        ukf.update(z);  _stabilise_P(ukf)
    ukf.predict(); _stabilise_P(ukf)
    return ukf


def build_ukfs(df: pd.DataFrame, symbol: str):
    p = ({'alpha': 0.001, 'beta': 7.0, 'kappa': 0, 'P': 0.001, 'Q': 1.0, 'R': 0.01}
         if symbol == 'BTC' else
         {'alpha': 0.001, 'beta': 4.0, 'kappa': 1, 'P': 0.1,   'Q': 1.0, 'R': 0.01})
    a, b, k, P, Q, R = p['alpha'], p['beta'], p['kappa'], p['P'], p['Q'], p['R']
    return (_train_ukf(df['close'].values, a, b, k, P, Q, R),
            _train_ukf(df['high'].values,  a, b, k, P, Q, R),
            _train_ukf(df['low'].values,   a, b, k, P, Q, R))


def step_ukf(ukf, high_ukf, low_ukf, row: pd.Series):
    for u, val in [(ukf, row['close']), (high_ukf, row['high']), (low_ukf, row['low'])]:
        u.update(val); _stabilise_P(u)
        u.predict();   _stabilise_P(u)
    return ukf.x[0], high_ukf.x[0], low_ukf.x[0]

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — SIGNAL GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def generate_signal(price, pred, high_pred, low_pred, symbol, balance,
                    leverage, strict, stricts, atr=0.0, atr_mult=None):
    """Generates a trade signal from UKF predictions. Returns a signal dict or None."""
    is_btc = (symbol == 'BTC')
    r      = 0 if is_btc else 2
    tp_inc = 1.004 if is_btc else 1.05

    if pred > price:
        pct_diff = abs(1 - pred / price)
        vol      = abs(1 - high_pred / pred) if pred > 0 else 0
        if not (pct_diff <= stricts or vol >= stricts):
            return None
        buy_price = low_pred
        if buy_price <= 0 or not (pred > buy_price and
                abs(1 - pred / buy_price) >= strict):
            return None
        entry = round(price if price < buy_price else buy_price, r)
        order_type = 'Market' if price < buy_price else 'Limit'
        tp_qty = (balance * leverage) / entry if entry > 0 else 1
        tp_cand = ((balance * tp_inc) * leverage) / tp_qty
        tp  = round(tp_cand if pred > tp_cand else pred, r)
        sl  = round(sl_price(entry, 'Buy', symbol, atr, atr_mult), r)
        return dict(symbol=symbol, order_type=order_type, order_side='Buy',
                    entry_price=entry, tp_price=tp, sl_price=sl,
                    current_price=price, atr=atr)

    elif price > pred:
        pct_diff = abs(1 - price / pred)
        vol      = abs(1 - pred / low_pred) if low_pred > 0 else 0
        if not (pct_diff <= stricts or vol >= stricts):
            return None
        sell_price = high_pred
        if sell_price <= 0 or not (sell_price > pred and
                abs(1 - sell_price / pred) >= strict):
            return None
        entry = round(price if price > sell_price else sell_price, r)
        order_type = 'Market' if price > sell_price else 'Limit'
        tp_qty = (balance * leverage) / entry if entry > 0 else 1
        tp_cand = ((balance * (2 - tp_inc)) * leverage) / tp_qty
        tp  = round(tp_cand if pred < tp_cand else pred, r)
        sl  = round(sl_price(entry, 'Sell', symbol, atr, atr_mult), r)
        return dict(symbol=symbol, order_type=order_type, order_side='Sell',
                    entry_price=entry, tp_price=tp, sl_price=sl,
                    current_price=price, atr=atr)

    return None

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — PARAMETER OPTIMISATION
# ══════════════════════════════════════════════════════════════════════════════

def _evaluate_params(btc_preds, eth_preds, btc_bars, eth_bars,
                      balance, leverage, sb, sbs, se, ses, atr_mult=None):
    """
    Shared evaluation core for both Bayesian and grid search.

    FIX 1 applied: Limit entries are rejected if the entry price never
    actually traded within the bar's [low, high] range.

    FIX 2 applied: BTC and ETH are evaluated and entered independently.
    Previously `if in_b or in_e: continue` blocked either symbol from
    entering whenever the other had an open position.

    This function evaluates a single, continuous, contiguous window (the
    grid-search/opt window) — there is no day-boundary chunking here, so
    the ATR cold-start issue (FIX 4) does not apply to this function; it
    only affects paper_trade_day's per-day re-chunking of the test period.

    At the end of the window, any still-open position is marked-to-market
    at the last available close (no exit fees/slippage applied) purely for
    ranking purposes — this function is a parameter-quality heuristic, not
    the authoritative equity curve, so it intentionally does not replicate
    the "carry across boundary, force-close only once" behaviour that
    paper_trade_day now uses (FIX 5). That behaviour lives in the main
    simulation loop where it actually matters for the reported return.
    """
    equity = balance
    in_b = in_e = False
    side_b = side_e = ''
    ep_b = ep_e = qty_b = qty_e = 0.0
    tp_b = sl_b = tp_e = sl_e = 0.0
    n = min(len(btc_preds), len(btc_bars), len(eth_preds), len(eth_bars))

    for i in range(n):
        rb = btc_bars.iloc[i]; re = eth_bars.iloc[i]
        pb, hb, lb = rb['close'], rb['high'], rb['low']
        pe, he, le = re['close'], re['high'], re['low']
        pred_b, hp_b, lp_b = btc_preds[i]
        pred_e, hp_e, lp_e = eth_preds[i]

        # BTC exit — SL checked first (pessimistic)
        if in_b:
            h_sl = lb <= sl_b if side_b == 'Buy' else hb >= sl_b
            h_tp = hb >= tp_b if side_b == 'Buy' else lb <= tp_b
            if h_sl and h_tp:
                h_tp = False
            if h_sl or h_tp:
                leg  = 'SL' if h_sl else 'TP'
                trig = sl_b if h_sl else tp_b
                xp, xc = apply_exit(trig, leg, side_b, qty_b)
                equity += compute_profit(side_b, ep_b, xp, qty_b) - xc
                in_b = False

        # ETH exit
        if in_e:
            h_sl = le <= sl_e if side_e == 'Buy' else he >= sl_e
            h_tp = he >= tp_e if side_e == 'Buy' else le <= tp_e
            if h_sl and h_tp:
                h_tp = False
            if h_sl or h_tp:
                leg  = 'SL' if h_sl else 'TP'
                trig = sl_e if h_sl else tp_e
                xp, xc = apply_exit(trig, leg, side_e, qty_e)
                equity += compute_profit(side_e, ep_e, xp, qty_e) - xc
                in_e = False

        atr_b = compute_atr(btc_bars.iloc[max(0, i - ATR_PERIOD):i + 1]) if USE_ATR_SL else 0.0
        atr_e = compute_atr(eth_bars.iloc[max(0, i - ATR_PERIOD):i + 1]) if USE_ATR_SL else 0.0

        # FIX 2 — independent per-symbol signal evaluation
        sig_b = generate_signal(pb, pred_b, hp_b, lp_b, 'BTC', equity, leverage,
                                 sb, sbs, atr=atr_b, atr_mult=atr_mult) if not in_b else None
        sig_e = generate_signal(pe, pred_e, hp_e, lp_e, 'ETH', equity, leverage,
                                 se, ses, atr=atr_e, atr_mult=atr_mult) if not in_e else None

        # FIX 1 — reject Limit entries that never actually traded this bar
        if sig_b and not is_fillable(sig_b['entry_price'], lb, hb):
            sig_b = None
        if sig_e and not is_fillable(sig_e['entry_price'], le, he):
            sig_e = None

        want_b = sig_b is not None
        want_e = sig_e is not None
        if not (want_b or want_e):
            continue

        if want_b and want_e:
            alloc_b = alloc_e = equity / 2
        elif want_b:
            alloc_b, alloc_e = equity * 0.8, 0.0
        else:
            alloc_b, alloc_e = 0.0, equity * 0.8

        if want_b:
            ep, qty, ec, valid = apply_entry(sig_b['entry_price'], sig_b['order_side'],
                                              alloc_b, leverage, BTC_LOT_STEP)
            if valid:
                in_b = True; side_b = sig_b['order_side']
                ep_b = ep; qty_b = qty; tp_b = sig_b['tp_price']
                sl_b = sl_price(ep, side_b, 'BTC', atr_b, atr_mult)
                equity -= ec

        if want_e:
            ep, qty, ec, valid = apply_entry(sig_e['entry_price'], sig_e['order_side'],
                                              alloc_e, leverage, ETH_LOT_STEP)
            if valid:
                in_e = True; side_e = sig_e['order_side']
                ep_e = ep; qty_e = qty; tp_e = sig_e['tp_price']
                sl_e = sl_price(ep, side_e, 'ETH', atr_e, atr_mult)
                equity -= ec

    # Mark-to-market any still-open position at window end (ranking only)
    if in_b:
        equity += compute_profit(side_b, ep_b, btc_bars.iloc[-1]['close'], qty_b)
    if in_e:
        equity += compute_profit(side_e, ep_e, eth_bars.iloc[-1]['close'], qty_e)

    return (equity / balance - 1) * 100


def run_bayesian_search(btc_preds, eth_preds, btc_bars, eth_bars,
                         balance, leverage, n_trials=BAYESIAN_TRIALS,
                         warm_start: dict = None):
    """Bayesian parameter optimisation using Optuna TPE sampler."""
    def objective(trial):
        sb  = trial.suggest_float('strict_btc',  0.002,  0.006,  step=0.00005)
        sbs = trial.suggest_float('stricts_btc', 0.003,  0.009,  step=0.00005)
        se  = trial.suggest_float('strict_eth',  0.0025, 0.007,  step=0.00005)
        ses = trial.suggest_float('stricts_eth', 0.003,  0.009,  step=0.00005)
        am  = (trial.suggest_float('atr_mult', ATR_SL_RANGE[0], ATR_SL_RANGE[1],
                                    step=0.1)
               if USE_ATR_SL else None)
        ret = _evaluate_params(btc_preds, eth_preds, btc_bars, eth_bars,
                                balance, leverage, sb, sbs, se, ses, am)
        return ret if ret > -np.inf else -999.0

    study = optuna.create_study(
        direction='maximize',
        sampler=optuna.samplers.TPESampler(seed=42, n_startup_trials=20))

    if warm_start is not None:
        trial_params = {k: warm_start[k]
                        for k in ('strict_btc', 'stricts_btc', 'strict_eth', 'stricts_eth')
                        if k in warm_start}
        if USE_ATR_SL and 'atr_mult' in warm_start:
            trial_params['atr_mult'] = warm_start['atr_mult']
        if trial_params:
            study.enqueue_trial(trial_params)

    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    best = study.best_params
    return {
        'strict_btc':   best['strict_btc'],
        'stricts_btc':  best['stricts_btc'],
        'strict_eth':   best['strict_eth'],
        'stricts_eth':  best['stricts_eth'],
        'atr_mult':     best.get('atr_mult', ATR_SL_MULTIPLIER),
        'best_returns': study.best_value,
        'n_trials':     len(study.trials),
    }


def run_grid_search(btc_preds, eth_preds, btc_bars, eth_bars, balance, leverage):
    """Blind grid search fallback (used when Optuna is not available)."""
    best_ret, best_params = -np.inf, None
    for sb, sbs, se, ses in product(STRICT_BTC_VALS, STRICTS_BTC_VALS,
                                     STRICT_ETH_VALS, STRICTS_ETH_VALS):
        ret = _evaluate_params(btc_preds, eth_preds, btc_bars, eth_bars,
                                balance, leverage, sb, sbs, se, ses,
                                ATR_SL_MULTIPLIER if USE_ATR_SL else None)
        if ret > best_ret and ret > 0:
            best_ret = ret
            best_params = dict(strict_btc=sb, stricts_btc=sbs,
                                strict_eth=se, stricts_eth=ses,
                                atr_mult=ATR_SL_MULTIPLIER,
                                best_returns=ret, n_trials=None)
    return best_params


def smooth_params(new_params: dict, prev_params: dict,
                  alpha: float = PARAM_SMOOTHING_ALPHA) -> dict:
    """Blends today's optimal params with yesterday's to dampen regime noise."""
    if prev_params is None:
        return new_params
    smoothed = dict(new_params)
    for k in ('strict_btc', 'stricts_btc', 'strict_eth', 'stricts_eth', 'atr_mult'):
        if k in new_params and k in prev_params:
            smoothed[k] = alpha * new_params[k] + (1 - alpha) * prev_params[k]
    return smoothed

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — PAPER TRADING LOOP (single day, with cross-day position carry)
# ══════════════════════════════════════════════════════════════════════════════

def paper_trade_day(btc_day, eth_day, btc_filters, eth_filters,
                     params, equity, leverage,
                     in_b=False, pos_b=None, in_e=False, pos_e=None,
                     btc_atr_warmup=None, eth_atr_warmup=None):
    """
    Simulates one day of live trading, bar by bar, with full verbose logging.
    Advances UKF state in-place.

    FIX 5 — Position state now flows IN and OUT of this function via
    in_b/pos_b/in_e/pos_e. There is NO end-of-day force-close inside this
    function anymore. A position opened on day 3 that hasn't hit TP/SL/
    reversal by the last bar of day 3 simply continues into day 4 exactly
    as it would live — hourly_task and one_minute_task never liquidate a
    healthy position just because the clock crossed midnight.

    FIX 4 — btc_atr_warmup/eth_atr_warmup carry the last ATR_PERIOD bars of
    the preceding window (training data for day 1, or naturally the actual
    previous day's bars for day 2+) purely for ATR lookback continuity.
    They are NEVER used for trading decisions — only compute_atr() sees them.

    Returns: (final_equity, trades_list, equity_trace, in_b, pos_b, in_e, pos_e)
    The last four are the carry-forward state for the next call.
    """
    pos_b = dict(pos_b) if pos_b else {}
    pos_e = dict(pos_e) if pos_e else {}

    btc_ukf_c, btc_ukf_h, btc_ukf_l = btc_filters
    eth_ukf_c, eth_ukf_h, eth_ukf_l = eth_filters
    sb, sbs = params['strict_btc'], params['stricts_btc']
    se, ses = params['strict_eth'], params['stricts_eth']
    atr_mult = params.get('atr_mult', ATR_SL_MULTIPLIER)

    if in_b:
        print(f"  [CARRY BTC]  Position from prior period carried in: "
              f"{pos_b.get('side','?')} @ ${pos_b.get('entry_price',0):,.2f}")
    if in_e:
        print(f"  [CARRY ETH]  Position from prior period carried in: "
              f"{pos_e.get('side','?')} @ ${pos_e.get('entry_price',0):,.4f}")

    trades = []
    equity_trace = []

    for idx in range(len(btc_day)):
        row_b = btc_day.iloc[idx]; row_e = eth_day.iloc[idx]
        ts = str(row_b.get('timestamp', row_b.name))

        pred_b, hp_b, lp_b = step_ukf(btc_ukf_c, btc_ukf_h, btc_ukf_l, row_b)
        pred_e, hp_e, lp_e = step_ukf(eth_ukf_c, eth_ukf_h, eth_ukf_l, row_e)

        pb, hb, lb = row_b['close'], row_b['high'], row_b['low']
        pe, he, le = row_e['close'], row_e['high'], row_e['low']

        # FIX 4 — ATR computed with warmup carryover so the first bars of
        # this window have a full-length lookback, not a cold start.
        if USE_ATR_SL:
            warm_b = btc_atr_warmup if btc_atr_warmup is not None else btc_day.iloc[0:0]
            warm_e = eth_atr_warmup if eth_atr_warmup is not None else eth_day.iloc[0:0]
            combined_b = pd.concat([warm_b, btc_day.iloc[:idx + 1]]).tail(ATR_PERIOD + 1)
            combined_e = pd.concat([warm_e, eth_day.iloc[:idx + 1]]).tail(ATR_PERIOD + 1)
            atr_b = compute_atr(combined_b)
            atr_e = compute_atr(combined_e)
        else:
            atr_b = atr_e = 0.0

        # ─── BTC EXIT ───────────────────────────────────────────────────────
        if in_b and pos_b:
            side = pos_b['side']; ep = pos_b['entry_price']
            qty  = pos_b['qty'];  tp = pos_b['tp_price']; sl = pos_b['sl_price']

            h_sl = lb <= sl if side == 'Buy' else hb >= sl
            h_tp = hb >= tp if side == 'Buy' else lb <= tp

            exit_leg = exit_trig = None
            if h_sl and h_tp:
                exit_leg, exit_trig = (('SL', sl) if USE_PESSIMISTIC_EXITS
                                        else ('TP', tp))
            elif h_sl: exit_leg, exit_trig = 'SL', sl
            elif h_tp: exit_leg, exit_trig = 'TP', tp

            if not exit_leg:
                pnl_check = compute_profit(side, ep, hb if side == 'Buy' else lb, qty)
                if pnl_check >= signal_math.profit_target_threshold('BTC', qty, ep, leverage):
                    exit_leg  = 'MARKET'
                    exit_trig = hb if side == 'Buy' else lb

            if exit_leg:
                xp, xc = (apply_exit(exit_trig, exit_leg, side, qty)
                           if exit_leg in ('SL', 'TP') else
                           market_exit(exit_trig, qty))
                pnl_raw = compute_profit(side, ep, xp, qty)
                fc      = funding_cost(pos_b['ts'], ts, qty, ep)
                net_pnl = pnl_raw - xc - fc
                equity += net_pnl
                trades.append({**pos_b, 'exit_price': xp, 'exit_ts': ts,
                                'exit_leg': exit_leg, 'profit': net_pnl,
                                'funding': fc, 'exit_commission': xc})
                in_b = False; pos_b = {}
                print(f"  [EXIT  BTC]  {ts} | {side} closed via {exit_leg:7s} "
                      f"@ ${xp:>10,.2f} | PnL: ${net_pnl:>+8.4f} | "
                      f"Funding: ${fc:.4f} | Balance: ${equity:.4f}")

        # ─── ETH EXIT ───────────────────────────────────────────────────────
        if in_e and pos_e:
            side = pos_e['side']; ep = pos_e['entry_price']
            qty  = pos_e['qty'];  tp = pos_e['tp_price']; sl = pos_e['sl_price']

            h_sl = le <= sl if side == 'Buy' else he >= sl
            h_tp = he >= tp if side == 'Buy' else le <= tp

            exit_leg = exit_trig = None
            if h_sl and h_tp:
                exit_leg, exit_trig = (('SL', sl) if USE_PESSIMISTIC_EXITS
                                        else ('TP', tp))
            elif h_sl: exit_leg, exit_trig = 'SL', sl
            elif h_tp: exit_leg, exit_trig = 'TP', tp

            if not exit_leg:
                pnl_check = compute_profit(side, ep, he if side == 'Buy' else le, qty)
                if pnl_check >= signal_math.profit_target_threshold('ETH', qty, ep, leverage):
                    exit_leg  = 'MARKET'
                    exit_trig = he if side == 'Buy' else le

            if exit_leg:
                xp, xc = (apply_exit(exit_trig, exit_leg, side, qty)
                           if exit_leg in ('SL', 'TP') else
                           market_exit(exit_trig, qty))
                pnl_raw = compute_profit(side, ep, xp, qty)
                fc      = funding_cost(pos_e['ts'], ts, qty, ep)
                net_pnl = pnl_raw - xc - fc
                equity += net_pnl
                trades.append({**pos_e, 'exit_price': xp, 'exit_ts': ts,
                                'exit_leg': exit_leg, 'profit': net_pnl,
                                'funding': fc, 'exit_commission': xc})
                in_e = False; pos_e = {}
                print(f"  [EXIT  ETH]  {ts} | {side} closed via {exit_leg:7s} "
                      f"@ ${xp:>10,.2f} | PnL: ${net_pnl:>+8.4f} | "
                      f"Funding: ${fc:.4f} | Balance: ${equity:.4f}")

        # ─── ENTRY SIGNALS (FIX 2 — independent per symbol) ─────────────────
        sig_b = (generate_signal(pb, pred_b, hp_b, lp_b, 'BTC', equity, leverage,
                                  sb, sbs, atr=atr_b, atr_mult=atr_mult)
                  if not in_b else None)
        sig_e = (generate_signal(pe, pred_e, hp_e, lp_e, 'ETH', equity, leverage,
                                  se, ses, atr=atr_e, atr_mult=atr_mult)
                  if not in_e else None)

        # FIX 1 — reject unfillable Limit entries
        if sig_b and not is_fillable(sig_b['entry_price'], lb, hb):
            print(f"  [SKIPPED]    {ts} | BTC {sig_b['order_side']} limit "
                  f"@ ${sig_b['entry_price']:,.2f} never traded "
                  f"(bar range ${lb:,.2f}-${hb:,.2f})")
            sig_b = None
        if sig_e and not is_fillable(sig_e['entry_price'], le, he):
            print(f"  [SKIPPED]    {ts} | ETH {sig_e['order_side']} limit "
                  f"@ ${sig_e['entry_price']:,.4f} never traded "
                  f"(bar range ${le:,.4f}-${he:,.4f})")
            sig_e = None

        want_b = sig_b is not None
        want_e = sig_e is not None

        if want_b and want_e:
            alloc_b = alloc_e = equity / 2
        elif want_b:
            alloc_b, alloc_e = equity * 0.8, 0.0
        elif want_e:
            alloc_b, alloc_e = 0.0, equity * 0.8
        else:
            alloc_b = alloc_e = 0.0

        if want_b:
            ep, qty, ec, valid = apply_entry(sig_b['entry_price'],
                                              sig_b['order_side'],
                                              alloc_b, leverage, BTC_LOT_STEP)
            if valid:
                equity -= ec
                actual_sl = round(sl_price(ep, sig_b['order_side'],
                                            'BTC', atr_b, atr_mult), 0)
                pos_b = dict(symbol='BTC', side=sig_b['order_side'],
                             entry_price=ep, qty=qty, tp_price=sig_b['tp_price'],
                             sl_price=actual_sl, ts=ts, entry_commission=ec,
                             trigger_price=sig_b['entry_price'],
                             atr_at_entry=atr_b)
                in_b = True
                sl_method = f"ATR×{atr_mult:.1f}={atr_b*atr_mult:.0f}" if USE_ATR_SL else "FIXED"
                print(f"  [ENTRY BTC]  {ts} | {sig_b['order_side']:4s} @ ${ep:>10,.2f} "
                      f"| TP: ${sig_b['tp_price']:>10,.2f} "
                      f"| SL: ${actual_sl:>10,.2f} ({sl_method}) "
                      f"| Qty: {qty:.3f} | Alloc: ${alloc_b:.4f}")
            else:
                print(f"  [REJECTED]   {ts} | BTC signal below MIN_NOTIONAL at alloc ${alloc_b:.4f}")

        if want_e:
            ep, qty, ec, valid = apply_entry(sig_e['entry_price'],
                                              sig_e['order_side'],
                                              alloc_e, leverage, ETH_LOT_STEP)
            if valid:
                equity -= ec
                actual_sl = round(sl_price(ep, sig_e['order_side'],
                                            'ETH', atr_e, atr_mult), 2)
                pos_e = dict(symbol='ETH', side=sig_e['order_side'],
                             entry_price=ep, qty=qty, tp_price=sig_e['tp_price'],
                             sl_price=actual_sl, ts=ts, entry_commission=ec,
                             trigger_price=sig_e['entry_price'],
                             atr_at_entry=atr_e)
                in_e = True
                sl_method = f"ATR×{atr_mult:.1f}={atr_e*atr_mult:.2f}" if USE_ATR_SL else "FIXED"
                print(f"  [ENTRY ETH]  {ts} | {sig_e['order_side']:4s} @ ${ep:>10,.4f} "
                      f"| TP: ${sig_e['tp_price']:>10,.4f} "
                      f"| SL: ${actual_sl:>10,.4f} ({sl_method}) "
                      f"| Qty: {qty:.3f} | Alloc: ${alloc_e:.4f}")
            else:
                print(f"  [REJECTED]   {ts} | ETH signal below MIN_NOTIONAL at alloc ${alloc_e:.4f}")

        equity_trace.append(equity)

    # NO EOD FORCE CLOSE (FIX 5). Whatever is open at the end of this day's
    # bars simply carries into the next call via the returned state.
    return equity, trades, equity_trace, in_b, pos_b, in_e, pos_e

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — METRICS
# ══════════════════════════════════════════════════════════════════════════════

def compute_metrics(trades, capital_curve, initial_capital):
    if not trades:
        return pd.DataFrame()
    profits = [t['profit'] for t in trades]
    wins    = [p for p in profits if p > 0]
    losses  = [p for p in profits if p <= 0]
    total   = len(profits)

    tp_ex    = sum(1 for t in trades if t.get('exit_leg') == 'TP')
    sl_ex    = sum(1 for t in trades if t.get('exit_leg') == 'SL')
    mk_ex    = sum(1 for t in trades if t.get('exit_leg') in ('MARKET', 'REVERSAL'))
    final_ex = sum(1 for t in trades if t.get('exit_leg') == 'FINAL')

    total_funding = sum(t.get('funding', 0) for t in trades)
    total_comm    = sum(t.get('entry_commission', 0) + t.get('exit_commission', 0)
                        for t in trades)

    curve = np.array(capital_curve)
    peak  = np.maximum.accumulate(curve)
    dd    = (curve - peak) / np.where(peak != 0, peak, 1)
    max_dd = abs(dd.min()) * 100

    if len(capital_curve) >= 48:
        daily_caps = capital_curve[24::24]
        daily_ret  = np.diff(daily_caps) / np.array(daily_caps[:-1])
        sharpe = (np.mean(daily_ret) / np.std(daily_ret) * np.sqrt(365)
                  if np.std(daily_ret) > 0 else 0.0)
    else:
        sharpe = 0.0

    slips = []
    for t in trades:
        if t.get('trigger_price') and t.get('entry_price'):
            d = ((t['entry_price'] - t['trigger_price']) if t['side'] == 'Buy'
                 else (t['trigger_price'] - t['entry_price']))
            slips.append(d / t['trigger_price'] * 100)

    atr_trades = [t for t in trades if t.get('atr_at_entry', 0) > 0]
    avg_atr = np.mean([t['atr_at_entry'] for t in atr_trades]) if atr_trades else 0

    final     = capital_curve[-1]
    total_ret = (final / initial_capital - 1) * 100

    # Longest holding period, meaningful now that positions can span days
    hold_hours = []
    for t in trades:
        try:
            ed = pd.to_datetime(t['ts']); xd = pd.to_datetime(t['exit_ts'])
            hold_hours.append((xd - ed).total_seconds() / 3600)
        except Exception:
            pass
    max_hold = max(hold_hours) if hold_hours else 0
    avg_hold = np.mean(hold_hours) if hold_hours else 0

    rows = [
        ("Total Return (%)",           f"{total_ret:+.4f}"),
        ("Final Capital ($)",          f"{final:.6f}"),
        ("Total Trades",               total),
        ("Win Rate (%)",               f"{len(wins)/total*100:.1f}" if total else "—"),
        ("Avg Win ($)",                f"{np.mean(wins):.6f}" if wins else "—"),
        ("Avg Loss ($)",               f"{np.mean(losses):.6f}" if losses else "—"),
        ("Profit Factor",              f"{abs(sum(wins)/sum(losses)):.3f}" if losses and wins else "—"),
        ("Best Trade ($)",             f"{max(profits):.6f}"),
        ("Worst Trade ($)",            f"{min(profits):.6f}"),
        ("Max Drawdown (%)",           f"{max_dd:.2f}"),
        ("Sharpe Ratio (annualised)",  f"{sharpe:.3f}"),
        ("TP Exits",                   tp_ex),
        ("SL Exits",                   sl_ex),
        ("Market/Reversal Exits",      mk_ex),
        ("Final Period Closes",        final_ex),
        ("Avg Hold Time (hours)",      f"{avg_hold:.1f}"),
        ("Max Hold Time (hours)",      f"{max_hold:.1f}"),
        ("Total Funding ($)",          f"{total_funding:.6f}"),
        ("Total Commission ($)",       f"{total_comm:.6f}"),
        ("Total Costs ($)",            f"{total_funding + total_comm:.6f}"),
        ("Costs as % of Capital",      f"{(total_funding+total_comm)/initial_capital*100:.3f}%"),
        ("Avg Entry Slippage (%)",     f"{np.mean(slips):.4f}" if slips else "—"),
        ("SL Method",                  "ATR-Adaptive" if USE_ATR_SL else "Fixed Offset"),
        ("Avg ATR at Entry",           f"{avg_atr:.2f}" if atr_trades else "—"),
        ("Pessimistic Exits",          str(USE_PESSIMISTIC_EXITS)),
        ("Optimizer",                  "Bayesian (Optuna)" if (USE_BAYESIAN and OPTUNA_AVAILABLE) else "Grid Search"),
    ]
    return pd.DataFrame(rows, columns=["Metric", "Value"])

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — PLOTTING
# ══════════════════════════════════════════════════════════════════════════════

def plot_results(capital_curve, trades, daily_params_log, initial_capital):
    if not ENABLE_PLOTTING:
        return

    fig = plt.figure(figsize=(18, 15))
    gs  = gridspec.GridSpec(3, 3, figure=fig, hspace=0.45, wspace=0.35)

    ax1 = fig.add_subplot(gs[0, :])
    hrs = np.arange(len(capital_curve))
    ax1.plot(hrs, capital_curve, color='#2ecc71', linewidth=1.4, label='Portfolio')
    ax1.axhline(initial_capital, color='#e74c3c', linestyle='--', lw=0.9, label='Initial')
    ax1.fill_between(hrs, initial_capital, capital_curve,
                      where=[c >= initial_capital for c in capital_curve],
                      color='#2ecc71', alpha=0.13)
    ax1.fill_between(hrs, initial_capital, capital_curve,
                      where=[c < initial_capital for c in capital_curve],
                      color='#e74c3c', alpha=0.13)
    sl_label = "ATR-Adaptive SL" if USE_ATR_SL else "Fixed SL"
    opt_label = "Bayesian Opt" if (USE_BAYESIAN and OPTUNA_AVAILABLE) else "Grid Search"
    ax1.set_title(f'Walk-Forward Capital Curve | {sl_label} | {opt_label} | '
                   f'Positions Carry Across Days', fontsize=12, fontweight='bold')
    ax1.set_xlabel('Hour'); ax1.set_ylabel('Capital ($)')
    ax1.legend(); ax1.grid(alpha=0.3)

    ax2 = fig.add_subplot(gs[1, :2])
    curve = np.array(capital_curve)
    peak  = np.maximum.accumulate(curve)
    dd    = (curve - peak) / np.where(peak != 0, peak, 1) * 100
    ax2.fill_between(hrs, dd, 0, color='#e74c3c', alpha=0.55)
    ax2.plot(hrs, dd, color='#c0392b', lw=0.8)
    ax2.set_title('Drawdown (%)', fontsize=11, fontweight='bold')
    ax2.set_xlabel('Hour'); ax2.set_ylabel('Drawdown (%)'); ax2.grid(alpha=0.3)

    ax3 = fig.add_subplot(gs[1, 2])
    profits = [t['profit'] for t in trades]
    if profits:
        # FIX (Phase 0): matplotlib's hist() takes ONE color per DATASET,
        # not one per sample — the previous per-trade color list crashed
        # with "The 'color' keyword argument must have one color per
        # dataset" on any run with trades. Wins and losses are now two
        # separate, shared-bin datasets, each with its own single color.
        bins = np.histogram_bin_edges(profits, bins=min(30, len(profits)))
        wins = [p for p in profits if p > 0]
        losses = [p for p in profits if p <= 0]
        if wins:
            ax3.hist(wins, bins=bins, color='#2ecc71', edgecolor='white', lw=0.4)
        if losses:
            ax3.hist(losses, bins=bins, color='#e74c3c', edgecolor='white', lw=0.4)
    ax3.axvline(0, color='black', lw=0.9, linestyle='--')
    ax3.set_title('Trade PnL Distribution', fontsize=11, fontweight='bold')
    ax3.set_xlabel('Profit ($)'); ax3.set_ylabel('Count'); ax3.grid(alpha=0.3)

    ax4 = fig.add_subplot(gs[2, 0])
    legs = [t.get('exit_leg', 'UNK') for t in trades]
    lc   = {k: legs.count(k) for k in sorted(set(legs))}
    cm   = {'TP': '#2ecc71', 'SL': '#e74c3c', 'MARKET': '#f39c12',
             'REVERSAL': '#e67e22', 'FINAL': '#3498db', 'UNK': '#95a5a6'}
    bars_ax = ax4.bar(lc.keys(), lc.values(), color=[cm.get(k, '#95a5a6') for k in lc])
    for bar, count in zip(bars_ax, lc.values()):
        ax4.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.1,
                  str(count), ha='center', va='bottom', fontsize=9)
    ax4.set_title('Exit Type Breakdown', fontsize=11, fontweight='bold')
    ax4.set_ylabel('Count'); ax4.grid(alpha=0.3, axis='y')

    ax5 = fig.add_subplot(gs[2, 1])
    btc_t = [t['profit'] for t in trades if t.get('symbol') == 'BTC']
    eth_t = [t['profit'] for t in trades if t.get('symbol') == 'ETH']
    if btc_t: ax5.plot(np.cumsum(btc_t), label='BTC', color='#f39c12', lw=1.2)
    if eth_t: ax5.plot(np.cumsum(eth_t), label='ETH', color='#3498db', lw=1.2)
    ax5.axhline(0, color='black', lw=0.8, linestyle='--')
    ax5.set_title('Cumulative PnL by Symbol', fontsize=11, fontweight='bold')
    ax5.set_xlabel('Trade #'); ax5.set_ylabel('Cumulative PnL ($)')
    ax5.legend(); ax5.grid(alpha=0.3)

    ax6 = fig.add_subplot(gs[2, 2])
    if daily_params_log:
        days = [p['day'] for p in daily_params_log]
        ax6.plot(days, [p.get('strict_btc', 0) for p in daily_params_log],
                  label='strict_btc', color='#f39c12', lw=0.9)
        ax6.plot(days, [p.get('strict_eth', 0) for p in daily_params_log],
                  label='strict_eth', color='#3498db', lw=0.9)
        if USE_ATR_SL:
            ax6_r = ax6.twinx()
            ax6_r.plot(days, [p.get('atr_mult', 0) for p in daily_params_log],
                        label='atr_mult', color='#9b59b6', lw=0.9, linestyle='--')
            ax6_r.set_ylabel('ATR Multiplier', color='#9b59b6', fontsize=8)
            ax6_r.legend(loc='lower right', fontsize=7)
    ax6.set_title('Daily Optimal Parameters', fontsize=11, fontweight='bold')
    ax6.set_xlabel('Test Day'); ax6.set_ylabel('Threshold')
    ax6.legend(loc='upper left', fontsize=7); ax6.grid(alpha=0.3)

    plt.suptitle('PyQuant v4 Walk-Forward — Production Backtest Engine',
                  fontsize=13, fontweight='bold', y=0.995)
    plt.savefig('walkforward_v4_results.png', dpi=150, bbox_inches='tight')
    plt.show()
    print("  Chart saved to walkforward_v4_results.png")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — MAIN WALK-FORWARD LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_walk_forward():
    global WALK_FORWARD_DAYS
    print("=" * 80)
    print("  PyQuant Walk-Forward Backtest Engine — v4 Production Edition")
    print(f"  Leverage: {LEVERAGE}x | Capital: ${INITIAL_CAPITAL} | Window: {WALK_FORWARD_DAYS} days")
    print(f"  Optimizer: {'Bayesian (Optuna)' if (USE_BAYESIAN and OPTUNA_AVAILABLE) else 'Grid Search'} | "
          f"SL: {'ATR-Adaptive' if USE_ATR_SL else 'Fixed Offset'} | "
          f"Pessimistic: {USE_PESSIMISTIC_EXITS}")
    print(f"  Positions carry across day boundaries — force-closed ONLY once, "
          f"at the end of the full walk-forward period.")
    if USE_ATR_SL:
        print(f"  ATR Period: {ATR_PERIOD}h | Multiplier range: {ATR_SL_RANGE} | "
              f"BTC floor: ${BTC_SL_FLOOR} | ETH floor: ${ETH_SL_FLOOR}")
    print("=" * 80)

    btc_full = pd.read_csv(BTC_CSV); eth_full = pd.read_csv(ETH_CSV)
    for df in (btc_full, eth_full):
        df.columns = [c.lower() for c in df.columns]
        t_col = next((c for c in df.columns if 'time' in c.lower()), None)
        if t_col:
            df['timestamp'] = pd.to_datetime(df[t_col])
            df.set_index('timestamp', inplace=True)

    common_idx = btc_full.index.intersection(eth_full.index)
    btc_full   = btc_full.loc[common_idx].reset_index()
    eth_full   = eth_full.loc[common_idx].reset_index()

    for col in ('open', 'high', 'low', 'close', 'volume'):
        if col not in btc_full.columns:
            raise ValueError(f"Missing column: '{col}'  |  Found: {list(btc_full.columns)}")

    bars_per_day = 24
    n_bars       = len(btc_full)
    train_bars   = UKF_TRAIN_DAYS * bars_per_day
    opt_bars     = GRID_SEARCH_DAYS * bars_per_day

    global WALK_FORWARD_DAYS
    if START_DATE or END_DATE:
        # Explicit date-range override (2026-10-31) — resolved against
        # this CSV's own 'timestamp' column rather than a day-count from
        # the newest bar. Reassigning n_bars/WALK_FORWARD_DAYS here means
        # every downstream use in this function (the day loop's bound
        # check, the post-loop force-close, the metrics dict, the print
        # statements) needs no further changes — they all already read
        # from these two names.
        ts = btc_full['timestamp']
        end_bar = n_bars
        if END_DATE:
            end_mask = ts <= (pd.Timestamp(END_DATE) + pd.Timedelta(hours=23, minutes=59))
            if not end_mask.any():
                raise ValueError(
                    f"END_DATE {END_DATE} is earlier than this dataset's first bar ({ts.iloc[0]})."
                )
            end_bar = int(end_mask[::-1].idxmax()) + 1  # last True index + 1, exclusive bound
        if START_DATE:
            start_mask = ts >= pd.Timestamp(START_DATE)
            if not start_mask.any():
                raise ValueError(
                    f"START_DATE {START_DATE} is later than this dataset's last bar ({ts.iloc[-1]})."
                )
            start_bar = int(start_mask.idxmax())
        else:
            start_bar = max(train_bars, end_bar - (WALK_FORWARD_DAYS * bars_per_day))
        if start_bar >= end_bar:
            raise ValueError(f"START_DATE {START_DATE} is not before END_DATE {END_DATE}.")
        WALK_FORWARD_DAYS = max(1, (end_bar - start_bar) // bars_per_day)
        n_bars = end_bar
        print(f"  Explicit date range: {START_DATE or '(start of window)'} → {END_DATE or '(latest bar)'} "
              f"→ bars {start_bar}-{n_bars-1} ({WALK_FORWARD_DAYS} day(s))")
    else:
        start_bar = n_bars - (WALK_FORWARD_DAYS * bars_per_day)
        if start_bar < train_bars:
            start_bar = train_bars
            print(f"  ⚠  Dataset too short for full recent-data targeting. "
                  f"Adjusting start to bar {start_bar}.")

    print(f"\n  Total bars: {n_bars} (~{n_bars//24} days) | "
          f"Test window: bars {start_bar}-{n_bars-1}\n")

    capital       = INITIAL_CAPITAL
    capital_curve = [capital]
    all_trades    = []
    params_log    = []
    prev_params   = None

    # FIX 5 — position state persists across the day loop instead of being
    # force-closed and reset every iteration.
    in_b, pos_b = False, {}
    in_e, pos_e = False, {}

    # Tracked so the single final force-close (after the loop) has a price
    # to close against, and so a failed/skipped day doesn't lose this reference.
    last_btc_close, last_btc_ts = None, None
    last_eth_close, last_eth_ts = None, None

    default_params = dict(strict_btc=0.0025, stricts_btc=0.005,
                           strict_eth=0.0035, stricts_eth=0.0075,
                           atr_mult=ATR_SL_MULTIPLIER, best_returns=0.0)

    for day_idx in range(WALK_FORWARD_DAYS):
        test_start = start_bar + day_idx * bars_per_day
        test_end   = test_start + bars_per_day
        if test_end > n_bars:
            break

        print(f"\n{'─'*80}")
        print(f"  ▶ Day {day_idx+1}/{WALK_FORWARD_DAYS} | "
              f"Bars {test_start}-{test_end-1} | Balance: ${capital:.4f}")
        print(f"{'─'*80}")

        btc_train = btc_full.iloc[max(0, test_start - train_bars):test_start]
        eth_train = eth_full.iloc[max(0, test_start - train_bars):test_start]
        btc_test  = btc_full.iloc[test_start:test_end].reset_index(drop=True)
        eth_test  = eth_full.iloc[test_start:test_end].reset_index(drop=True)

        try:
            btc_filters = build_ukfs(btc_train, 'BTC')
            eth_filters = build_ukfs(eth_train, 'ETH')
        except Exception as e:
            print(f"  ✗  UKF training failed: {e} — skipping day.")
            capital_curve.extend([capital] * bars_per_day)
            continue

        opt_start_idx = max(0, len(btc_train) - opt_bars)
        btc_opt = btc_train.iloc[opt_start_idx:].reset_index(drop=True)
        eth_opt = eth_train.iloc[opt_start_idx:].reset_index(drop=True)
        btc_pre = btc_train.iloc[:opt_start_idx]
        eth_pre = eth_train.iloc[:opt_start_idx]

        try:
            pre_b = build_ukfs(btc_pre, 'BTC') if len(btc_pre) >= 10 else btc_filters
            pre_e = build_ukfs(eth_pre, 'ETH') if len(eth_pre) >= 10 else eth_filters
            b_preds = [step_ukf(pre_b[0], pre_b[1], pre_b[2], btc_opt.iloc[i])
                       for i in range(len(btc_opt))]
            e_preds = [step_ukf(pre_e[0], pre_e[1], pre_e[2], eth_opt.iloc[i])
                       for i in range(len(eth_opt))]
        except Exception as e:
            print(f"  ✗  Prediction pre-computation failed: {e}")
            b_preds = e_preds = []

        t_opt = time.time()
        current_params = None
        if b_preds and e_preds:
            try:
                if USE_BAYESIAN and OPTUNA_AVAILABLE:
                    current_params = run_bayesian_search(
                        b_preds, e_preds, btc_opt, eth_opt,
                        capital, LEVERAGE, BAYESIAN_TRIALS, warm_start=prev_params)
                    print(f"  [BAYESIAN]  Completed {current_params['n_trials']} trials "
                          f"in {time.time()-t_opt:.1f}s | "
                          f"Best return: {current_params['best_returns']:+.2f}%")
                else:
                    current_params = run_grid_search(
                        b_preds, e_preds, btc_opt, eth_opt, capital, LEVERAGE)
                    if current_params:
                        print(f"  [GRID]      Completed in {time.time()-t_opt:.1f}s | "
                              f"Best return: {current_params['best_returns']:+.2f}%")
            except Exception as e:
                print(f"  ✗  Optimizer error: {e}")

        if current_params is None:
            current_params = dict(default_params)
            print("  ⚠  No profitable params found — using factory defaults.")
        else:
            current_params = smooth_params(current_params, prev_params)

        atr_suffix = f"  ATR×{current_params.get('atr_mult', ATR_SL_MULTIPLIER):.2f}" if USE_ATR_SL else ''
        print(f"  [PARAMS]    BTC({current_params['strict_btc']:.5f} / "
              f"{current_params['stricts_btc']:.5f})  "
              f"ETH({current_params['strict_eth']:.5f} / "
              f"{current_params['stricts_eth']:.5f}){atr_suffix}")

        current_params['day'] = day_idx + 1
        params_log.append(dict(current_params))
        prev_params = dict(current_params)

        # FIX 4 — ATR warmup tail from the training window (or, from day 2
        # onward, this naturally represents the actual preceding real data).
        btc_atr_warmup = btc_train.tail(ATR_PERIOD) if USE_ATR_SL else None
        eth_atr_warmup = eth_train.tail(ATR_PERIOD) if USE_ATR_SL else None

        # FIX 3 — paper_trade_day is now defended. A failure here logs and
        # skips the day (capital unchanged, position state preserved exactly
        # as it was going in) instead of crashing the entire run.
        try:
            (final_cap, day_trades, day_equity_trace,
             in_b, pos_b, in_e, pos_e) = paper_trade_day(
                btc_test, eth_test, btc_filters, eth_filters,
                current_params, capital, LEVERAGE,
                in_b=in_b, pos_b=pos_b, in_e=in_e, pos_e=pos_e,
                btc_atr_warmup=btc_atr_warmup, eth_atr_warmup=eth_atr_warmup)
        except Exception as e:
            print(f"  ✗  paper_trade_day failed: {e} — day skipped, "
                  f"capital and open positions unchanged.")
            capital_curve.extend([capital] * bars_per_day)
            continue

        day_pnl  = final_cap - capital
        capital  = final_cap

        if len(day_equity_trace) == bars_per_day:
            capital_curve.extend(day_equity_trace)
        else:
            padded = day_equity_trace + [capital] * (bars_per_day - len(day_equity_trace))
            capital_curve.extend(padded[:bars_per_day])
        all_trades.extend(day_trades)

        # Track last known prices for the final force-close after the loop
        last_btc_close = btc_test.iloc[-1]['close']
        last_btc_ts    = str(btc_test.iloc[-1].get('timestamp', btc_test.index[-1]))
        last_eth_close = eth_test.iloc[-1]['close']
        last_eth_ts    = str(eth_test.iloc[-1].get('timestamp', eth_test.index[-1]))

        print(f"\n  Day {day_idx+1} summary | PnL: ${day_pnl:+.4f} | "
              f"Trades: {len(day_trades)} | Balance: ${capital:.4f} | "
              f"Open: BTC={'Y' if in_b else 'N'} ETH={'Y' if in_e else 'N'}")

    # ── FIX 5 — Single final force-close, only after the ENTIRE walk-forward
    # period has ended. This is the one and only point where an open position
    # is liquidated purely because the test is ending, not because of any
    # trading logic — exactly matching "close everything to get a clean final
    # number" without pretending the live system does this every day.
    print(f"\n{'─'*80}")
    print("  FINAL PERIOD CLOSE — liquidating any remaining open positions")
    print(f"{'─'*80}")

    if in_b and pos_b and last_btc_close is not None:
        xp, xc  = market_exit(last_btc_close, pos_b['qty'])
        fc      = funding_cost(pos_b['ts'], last_btc_ts, pos_b['qty'], pos_b['entry_price'])
        net_pnl = compute_profit(pos_b['side'], pos_b['entry_price'], xp, pos_b['qty']) - xc - fc
        capital += net_pnl
        all_trades.append({**pos_b, 'exit_price': xp, 'exit_ts': last_btc_ts,
                            'exit_leg': 'FINAL', 'profit': net_pnl,
                            'funding': fc, 'exit_commission': xc})
        if capital_curve:
            capital_curve[-1] = capital
        print(f"  [FINAL CLOSE] BTC {pos_b['side']} @ ${xp:,.2f} | "
              f"PnL: ${net_pnl:+.4f} | Balance: ${capital:.4f}")
        in_b = False

    if in_e and pos_e and last_eth_close is not None:
        xp, xc  = market_exit(last_eth_close, pos_e['qty'])
        fc      = funding_cost(pos_e['ts'], last_eth_ts, pos_e['qty'], pos_e['entry_price'])
        net_pnl = compute_profit(pos_e['side'], pos_e['entry_price'], xp, pos_e['qty']) - xc - fc
        capital += net_pnl
        all_trades.append({**pos_e, 'exit_price': xp, 'exit_ts': last_eth_ts,
                            'exit_leg': 'FINAL', 'profit': net_pnl,
                            'funding': fc, 'exit_commission': xc})
        if capital_curve:
            capital_curve[-1] = capital
        print(f"  [FINAL CLOSE] ETH {pos_e['side']} @ ${xp:,.4f} | "
              f"PnL: ${net_pnl:+.4f} | Balance: ${capital:.4f}")
        in_e = False

    if not (all_trades and all_trades[-1].get('exit_leg') == 'FINAL'):
        print("  No open positions remained at the end of the test period.")

    print("\n" + "=" * 80)
    print("  FINAL PERFORMANCE SUMMARY")
    print("=" * 80)
    metrics = compute_metrics(all_trades, capital_curve, INITIAL_CAPITAL)
    print(metrics.to_string(index=False))

    # Phase 2: log this run for the UI's "Backtest" screen. Purely
    # additive — a logging failure must never affect the run's own
    # output/return value, so it's wrapped and never re-raised. Skipped
    # when a caller is tracking its own pyquant_runs row (see
    # _SUPPRESS_AUTO_RECORD_RUN's definition above) — manual CLI usage
    # (the normal case) always hits this branch unchanged.
    if not _SUPPRESS_AUTO_RECORD_RUN:
        try:
            metrics_dict = dict(zip(metrics["Metric"], metrics["Value"])) if not metrics.empty else {}
            new_run_id = state_store.record_run(
                run_type="walkforward",
                metrics=metrics_dict,
                symbol="BTC+ETH",
                params={
                    "leverage": LEVERAGE, "initial_capital": INITIAL_CAPITAL,
                    "walk_forward_days": WALK_FORWARD_DAYS,
                    "use_bayesian": bool(USE_BAYESIAN and OPTUNA_AVAILABLE),
                    "use_atr_sl": USE_ATR_SL,
                    "use_pessimistic_exits": USE_PESSIMISTIC_EXITS,
                },
            )
            # 2026-10-28: persist per-trade data alongside the aggregate
            # metrics above — see trades_to_records()'s docstring. Kept
            # inside this same try/except: a logging failure here must
            # never affect the run's own output/return value, matching
            # record_run()'s own standing treatment just above.
            if new_run_id is not None:
                state_store.record_backtest_trades(new_run_id, trades_to_records(all_trades, LEVERAGE))
        except Exception as e:
            print(f"  (non-fatal) run logging failed: {e}")

    print(f"\n{'─'*80}")
    print(f"  Net: {((capital/INITIAL_CAPITAL)-1)*100:+.4f}%  |  "
          f"Final: ${capital:.4f}  |  "
          f"Trades: {len(all_trades)}")
    print(f"{'─'*80}")

    if ENABLE_PLOTTING:
        plot_results(capital_curve, all_trades, params_log, INITIAL_CAPITAL)

    return metrics, all_trades, capital_curve, params_log


if __name__ == '__main__':
    run_walk_forward()
