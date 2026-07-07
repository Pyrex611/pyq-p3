"""
p4_search.py
Daily parameter optimizer for PyQuant — Bayesian (Optuna) edition.

REFACTOR NOTE: This file previously ran a blind grid search over 2,744
parameter combinations (7 x 7 x 7 x 8), taking roughly 8-15 minutes per
daily run. It now uses Optuna's TPE (Tree-structured Parzen Estimator)
sampler, evaluating ~150 continuous-space trials in under 2 minutes, with
comparable or better parameter quality — this is the exact same approach
validated in pyquant_walkforward_v4.py, which produced +1060% / 91.4% win
rate / Sharpe 15.26 on the walk-forward backtest.

Also added: an `atr_mult` search dimension for the ATR-adaptive stop loss
(see SignalGenerator._calculate_sl_price in pyquant_utils.py), jointly
optimized with the four signal thresholds — exactly as in the validated
backtest, since a wider/narrower ATR multiplier changes which signal
thresholds are actually profitable.

The function name `run_grid_search()` is kept for interface compatibility
with pyquant_orchestra.py's daily_optimizer(), which calls it generically
and writes the result to optimal_params.json — no changes needed there.

This module keeps its OWN lightweight UKF implementation (not importing
UKFModel from pyquant_utils.py) to preserve the original architecture's
deliberate import isolation — pyquant_utils.py has no dependency on this
file, and this file has no dependency on pyq_p4.py, avoiding any risk of
a circular import between the orchestrator's three main modules.
"""

import os
import json
import math
import time
import numpy as np
import pandas as pd

from filterpy.kalman import UnscentedKalmanFilter, MerweScaledSigmaPoints
from filterpy.common import Q_discrete_white_noise

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    OPTUNA_AVAILABLE = True
except ImportError:
    OPTUNA_AVAILABLE = False
    print("[grid_search] WARNING: optuna not installed — falling back to "
          "blind grid search. Install with: pip install optuna")


# ── Configuration (matches pyquant_walkforward_v4.py exactly) ────────────────

LEVERAGE          = 15
BAYESIAN_TRIALS   = 150
PARAM_SMOOTHING_ALPHA = 0.7

USE_ATR_SL        = True
ATR_PERIOD        = 14
ATR_SL_RANGE      = (1.0, 3.0)
ATR_SL_MULTIPLIER = 1.8
BTC_SL_FLOOR      = 100.0
ETH_SL_FLOOR      = 8.0
BTC_SL_DELTA      = 7.5 * 30
ETH_SL_DELTA      = 16.0 * 1

MAKER_FEE            = 0.0002
TAKER_FEE            = 0.0005
ENTRY_SLIPPAGE_PCT   = 0.0003
TP_EXIT_SLIPPAGE_PCT = 0.0004
SL_EXIT_SLIPPAGE_PCT = 0.0008
MIN_NOTIONAL         = 5.0
BTC_LOT_STEP         = 0.001
ETH_LOT_STEP         = 0.001

# Grid fallback space (only used if Optuna is not installed)
STRICT_BTC_VALS  = [0.0025, 0.00275, 0.003, 0.0035, 0.004, 0.0045, 0.005]
STRICTS_BTC_VALS = [0.004,  0.0045,  0.005, 0.0055, 0.006, 0.007,  0.0075]
STRICT_ETH_VALS  = [0.0035, 0.004,   0.0045, 0.005, 0.0055, 0.006]
STRICTS_ETH_VALS = [0.0045, 0.005,   0.0055, 0.006, 0.0065, 0.007, 0.0075, 0.008]

BACKTEST_DAYS    = 15   # opt window
UKF_TRAIN_DAYS   = 30   # training window before the opt window


# ── Accounting helpers (identical formulas to pyquant_walkforward_v4.py) ─────

def floor_to_lot(qty, step):
    return math.floor(qty / step) * step


def is_fillable(entry_price, bar_low, bar_high):
    """FIX 1 (carried from the walk-forward validation) — reject Limit
    entries whose price never actually traded within the bar."""
    return bar_low <= entry_price <= bar_high


def apply_entry(signal_price, side, capital, leverage, lot_step):
    slip   = ENTRY_SLIPPAGE_PCT
    filled = signal_price * (1 + slip) if side == 'Buy' else signal_price * (1 - slip)
    qty    = floor_to_lot((capital * leverage) / filled, lot_step)
    valid  = qty > 0 and (qty * filled) >= MIN_NOTIONAL
    return filled, qty, MAKER_FEE * qty * filled, valid


def apply_exit(trigger, leg, side, qty):
    slip   = SL_EXIT_SLIPPAGE_PCT if leg == 'SL' else TP_EXIT_SLIPPAGE_PCT
    filled = trigger * (1 - slip) if side == 'Buy' else trigger * (1 + slip)
    return filled, TAKER_FEE * qty * filled


def compute_profit(side, entry, exit_p, qty):
    return (exit_p - entry) * qty if side == 'Buy' else (entry - exit_p) * qty


def compute_atr(df, period=ATR_PERIOD):
    if len(df) < 2:
        return 0.0
    h = df['high'].values; l = df['low'].values; c = df['close'].values
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]),
                               np.abs(l[1:] - c[:-1])))
    n = min(period, len(tr))
    return float(np.mean(tr[-n:])) if n > 0 else 0.0


def sl_price(entry, side, symbol, atr=0.0, atr_mult=None):
    is_btc      = (symbol == 'BTC')
    fixed_delta = BTC_SL_DELTA if is_btc else ETH_SL_DELTA
    floor_delta = BTC_SL_FLOOR if is_btc else ETH_SL_FLOOR
    if USE_ATR_SL and atr > 0 and atr_mult is not None:
        delta = max(atr_mult * atr, floor_delta)
    else:
        delta = fixed_delta
    return (entry - delta) if side == 'Buy' else (entry + delta)


# ── UKF engine (isolated copy, matches pyquant_walkforward_v4.py) ────────────

def _stabilise_P(ukf, floor=1e-6):
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


def build_ukfs(df, symbol):
    p = ({'alpha': 0.001, 'beta': 7.0, 'kappa': 0, 'P': 0.001, 'Q': 1.0, 'R': 0.01}
         if symbol == 'BTC' else
         {'alpha': 0.001, 'beta': 4.0, 'kappa': 1, 'P': 0.1,   'Q': 1.0, 'R': 0.01})
    a, b, k, P, Q, R = p['alpha'], p['beta'], p['kappa'], p['P'], p['Q'], p['R']
    return (_train_ukf(df['close'].values, a, b, k, P, Q, R),
            _train_ukf(df['high'].values,  a, b, k, P, Q, R),
            _train_ukf(df['low'].values,   a, b, k, P, Q, R))


def step_ukf(ukf, high_ukf, low_ukf, row):
    for u, val in [(ukf, row['close']), (high_ukf, row['high']), (low_ukf, row['low'])]:
        u.update(val); _stabilise_P(u)
        u.predict();   _stabilise_P(u)
    return ukf.x[0], high_ukf.x[0], low_ukf.x[0]


# ── Signal generation (mirrors SignalGenerator's math as a pure function) ───

def generate_signal(price, pred, high_pred, low_pred, symbol, balance, leverage,
                    strict, stricts, atr=0.0, atr_mult=None):
    is_btc = (symbol == 'BTC')
    r      = 0 if is_btc else 2
    tp_inc = 1.004 if is_btc else 1.05

    if pred > price:
        pct_diff = abs(1 - pred / price)
        vol      = abs(1 - high_pred / pred) if pred > 0 else 0
        if not (pct_diff <= stricts or vol >= stricts):
            return None
        buy_price = low_pred
        if buy_price <= 0 or not (pred > buy_price and abs(1 - pred / buy_price) >= strict):
            return None
        entry = round(price if price < buy_price else buy_price, r)
        tp_qty = (balance * leverage) / entry if entry > 0 else 1
        tp_cand = ((balance * tp_inc) * leverage) / tp_qty
        tp  = round(tp_cand if pred > tp_cand else pred, r)
        sl  = round(sl_price(entry, 'Buy', symbol, atr, atr_mult), r)
        return dict(symbol=symbol, order_side='Buy', entry_price=entry,
                    tp_price=tp, sl_price=sl)

    elif price > pred:
        pct_diff = abs(1 - price / pred)
        vol      = abs(1 - pred / low_pred) if low_pred > 0 else 0
        if not (pct_diff <= stricts or vol >= stricts):
            return None
        sell_price = high_pred
        if sell_price <= 0 or not (sell_price > pred and abs(1 - sell_price / pred) >= strict):
            return None
        entry = round(price if price > sell_price else sell_price, r)
        tp_qty = (balance * leverage) / entry if entry > 0 else 1
        tp_cand = ((balance * (2 - tp_inc)) * leverage) / tp_qty
        tp  = round(tp_cand if pred < tp_cand else pred, r)
        sl  = round(sl_price(entry, 'Sell', symbol, atr, atr_mult), r)
        return dict(symbol=symbol, order_side='Sell', entry_price=entry,
                    tp_price=tp, sl_price=sl)

    return None


# ── Evaluation core (shared by Bayesian and grid, matches walkforward v4) ────

def _evaluate_params(btc_preds, eth_preds, btc_bars, eth_bars,
                     balance, leverage, sb, sbs, se, ses, atr_mult=None):
    """
    FIX 1 applied — rejects Limit entries whose price never traded in the bar.
    FIX 2 applied — BTC and ETH are evaluated and entered independently.
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

        sig_b = generate_signal(pb, pred_b, hp_b, lp_b, 'BTC', equity, leverage,
                                 sb, sbs, atr=atr_b, atr_mult=atr_mult) if not in_b else None
        sig_e = generate_signal(pe, pred_e, hp_e, lp_e, 'ETH', equity, leverage,
                                 se, ses, atr=atr_e, atr_mult=atr_mult) if not in_e else None

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

    if in_b:
        equity += compute_profit(side_b, ep_b, btc_bars.iloc[-1]['close'], qty_b)
    if in_e:
        equity += compute_profit(side_e, ep_e, eth_bars.iloc[-1]['close'], qty_e)

    return (equity / balance - 1) * 100


def _run_bayesian(btc_preds, eth_preds, btc_bars, eth_bars, balance, leverage,
                  n_trials, warm_start=None):
    def objective(trial):
        sb  = trial.suggest_float('strict_btc',  0.002,  0.006,  step=0.00005)
        sbs = trial.suggest_float('stricts_btc', 0.003,  0.009,  step=0.00005)
        se  = trial.suggest_float('strict_eth',  0.0025, 0.007,  step=0.00005)
        ses = trial.suggest_float('stricts_eth', 0.003,  0.009,  step=0.00005)
        am  = (trial.suggest_float('atr_mult', ATR_SL_RANGE[0], ATR_SL_RANGE[1], step=0.1)
               if USE_ATR_SL else None)
        ret = _evaluate_params(btc_preds, eth_preds, btc_bars, eth_bars,
                                balance, leverage, sb, sbs, se, ses, am)
        return ret if ret > -np.inf else -999.0

    study = optuna.create_study(
        direction='maximize',
        sampler=optuna.samplers.TPESampler(seed=42, n_startup_trials=20))

    if warm_start is not None:
        trial_params = {k: warm_start[k] for k in
                        ('strict_btc', 'stricts_btc', 'strict_eth', 'stricts_eth')
                        if k in warm_start}
        if USE_ATR_SL and 'atr_mult' in warm_start:
            trial_params['atr_mult'] = warm_start['atr_mult']
        if trial_params:
            try:
                study.enqueue_trial(trial_params)
            except Exception as e:
                print(f"[grid_search] Warm-start enqueue failed (non-fatal): {e}")

    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    best = study.best_params
    return {
        'strict_btc':  best['strict_btc'],
        'stricts_btc': best['stricts_btc'],
        'strict_eth':  best['strict_eth'],
        'stricts_eth': best['stricts_eth'],
        'atr_mult':    best.get('atr_mult', ATR_SL_MULTIPLIER),
        'best_returns': study.best_value,
    }


def _run_grid_fallback(btc_preds, eth_preds, btc_bars, eth_bars, balance, leverage):
    from itertools import product
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
                                atr_mult=ATR_SL_MULTIPLIER, best_returns=ret)
    return best_params


def _smooth(new_params, prev_params, alpha=PARAM_SMOOTHING_ALPHA):
    if prev_params is None:
        return new_params
    smoothed = dict(new_params)
    for k in ('strict_btc', 'stricts_btc', 'strict_eth', 'stricts_eth', 'atr_mult'):
        if k in new_params and k in prev_params:
            smoothed[k] = alpha * new_params[k] + (1 - alpha) * prev_params[k]
    return smoothed


def _load_prev_params():
    """Reads yesterday's optimal_params.json to warm-start today's search."""
    try:
        with open("optimal_params.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ── Public entry point — same name/signature the orchestrator already calls ─

def run_grid_search(backtest_days: int = BACKTEST_DAYS, initial_capital: float = 10.0) -> dict:
    """
    Daily parameter optimizer. Despite the legacy name (kept for
    pyquant_orchestra.py compatibility), this now runs Bayesian optimization
    via Optuna rather than a blind grid — see module docstring.

    Reads btc_back366.csv / eth_back366.csv (written by data_download.py),
    trains UKFs on the last UKF_TRAIN_DAYS, generates predictions over the
    last `backtest_days`, and searches for the best strict/stricts/atr_mult
    combination. Falls back to the blind grid automatically if Optuna is
    not installed. Warm-starts from the previous day's optimal_params.json
    and smooths the result with it to dampen day-to-day parameter noise.
    """
    length = backtest_days * 24
    train_len = UKF_TRAIN_DAYS * 24

    btc_valid = pd.read_csv("btc_back366.csv")
    eth_valid = pd.read_csv("eth_back366.csv")

    train_start = max(0, len(btc_valid) - train_len - length)
    btc_train = btc_valid.iloc[train_start: len(btc_valid) - length].reset_index(drop=True)
    eth_train = eth_valid.iloc[train_start: len(eth_valid) - length].reset_index(drop=True)
    btc_opt   = btc_valid.iloc[len(btc_valid) - length:].reset_index(drop=True)
    eth_opt   = eth_valid.iloc[len(eth_valid) - length:].reset_index(drop=True)

    print(f"[grid_search] Training UKFs on {len(btc_train)} bars, "
          f"optimizing over {len(btc_opt)} bars…")

    start_time = time.time()
    try:
        btc_ukf, btc_high_ukf, btc_low_ukf = build_ukfs(btc_train, 'BTC')
        eth_ukf, eth_high_ukf, eth_low_ukf = build_ukfs(eth_train, 'ETH')
    except Exception as e:
        print(f"[grid_search] UKF training failed: {e}")
        return _fallback_result()

    try:
        btc_preds = [step_ukf(btc_ukf, btc_high_ukf, btc_low_ukf, btc_opt.iloc[i])
                     for i in range(len(btc_opt))]
        eth_preds = [step_ukf(eth_ukf, eth_high_ukf, eth_low_ukf, eth_opt.iloc[i])
                     for i in range(len(eth_opt))]
    except Exception as e:
        print(f"[grid_search] Prediction generation failed: {e}")
        return _fallback_result()

    prev_params = _load_prev_params()

    result = None
    if OPTUNA_AVAILABLE:
        try:
            result = _run_bayesian(btc_preds, eth_preds, btc_opt, eth_opt,
                                   initial_capital, LEVERAGE, BAYESIAN_TRIALS,
                                   warm_start=prev_params)
            print(f"[grid_search] Bayesian search completed "
                  f"({BAYESIAN_TRIALS} trials) in {time.time()-start_time:.1f}s. "
                  f"Best return: {result['best_returns']:.2f}%")
        except Exception as e:
            print(f"[grid_search] Bayesian search failed: {e} — using grid fallback.")

    if result is None:
        result = _run_grid_fallback(btc_preds, eth_preds, btc_opt, eth_opt,
                                    initial_capital, LEVERAGE)
        if result:
            print(f"[grid_search] Grid search completed in "
                  f"{time.time()-start_time:.1f}s. Best return: {result['best_returns']:.2f}%")

    if result is None:
        print("[grid_search] WARNING: No profitable combo found. Using defaults.")
        return _fallback_result()

    smoothed = _smooth(result, prev_params)
    print(f"[grid_search] Final params (after smoothing): "
          f"BTC({smoothed['strict_btc']:.5f}/{smoothed['stricts_btc']:.5f}) "
          f"ETH({smoothed['strict_eth']:.5f}/{smoothed['stricts_eth']:.5f}) "
          f"ATR×{smoothed.get('atr_mult', ATR_SL_MULTIPLIER):.2f}")
    return smoothed


def _fallback_result() -> dict:
    return {
        "strict_btc": 0.0025, "stricts_btc": 0.005,
        "strict_eth": 0.0035, "stricts_eth": 0.0075,
        "atr_mult": ATR_SL_MULTIPLIER,
        "best_returns": 0.0,
    }


if __name__ == "__main__":
    result = run_grid_search()
    print(result)
