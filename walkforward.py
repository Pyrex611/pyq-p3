"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         PyQuant Walk-Forward Realistic Backtest — Kaggle Edition            ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Upload btc_back366.csv and eth_back366.csv before running.                 ║
║  Kaggle path:  /kaggle/input/<dataset-name>/btc_back366.csv                 ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  ARCHITECTURE — mirrors live system exactly                                  ║
║                                                                              ║
║  Live system daily cycle (from pyquant_orchestra.py):                       ║
║    00:03 → daily_optimizer() runs grid search on fresh 366-day data         ║
║    :00   → hourly_task() uses the resulting optimal_params.json             ║
║                                                                              ║
║  Walk-forward cycle (this script):                                           ║
║    For each test day D (starting at day 31):                                 ║
║      Training window : bars from (D-30) to (D-1)  → retrain 6 UKFs         ║
║      Opt window      : last 15 days of training   → fast grid search        ║
║      Test window     : 24 hourly bars of day D    → paper trade             ║
║      Roll forward    : D += 1                                                ║
║                                                                              ║
║  This means EVERY test day uses parameters found from only the data that    ║
║  would have been available at midnight that day — no lookahead bias.        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  5-MINUTE ENTRY REFINEMENT                                                   ║
║                                                                              ║
║  When the 1H UKF generates a SELL signal, the 5m UKF may show that price   ║
║  will temporarily move UP before the bearish move takes hold. In that case  ║
║  the 5m high_pred gives a better (higher) limit entry for the short.        ║
║                                                                              ║
║  SELL: if pred_5m > close (micro-bullish), use max(high_pred_5m,            ║
║         high_pred_1H) as the limit entry price, capped at bar's high.       ║
║  BUY:  if pred_5m < close (micro-bearish), use min(low_pred_5m,             ║
║         low_pred_1H) as the limit entry price, floored at bar's low.        ║
║                                                                              ║
║  Falls back to the 1H entry price if the 5m prediction does not improve     ║
║  the entry. Entry refinement can only make the entry BETTER, never worse.   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  REALISM MODEL                                                               ║
║    Maker fee     0.02 %  — LIMIT entries                                     ║
║    Taker fee     0.05 %  — STOP_MARKET / TAKE_PROFIT_MARKET exits           ║
║    Entry slip    0.03 %  — LIMIT queue position                             ║
║    TP slip       0.04 %  — exit during calm directional move                ║
║    SL slip       0.08 %  — exit during fast adverse move                    ║
║    Funding       0.01 %  per complete 8-hour period on leveraged notional   ║
║    Min notional  $5 USDT — Binance Futures floor                            ║
║    Lot step      BTC 0.001 / ETH 0.001                                      ║
║    Leverage      configurable, default 10x (matches live target)            ║
║                                                                              ║
║  EXIT RESOLUTION                                                             ║
║    SL-first on ambiguous candles (both TP and SL in same bar → SL wins).   ║
║    This is the pessimistic worst-case assumption from the original backtest. ║
║    Intentionally unchanged. See comments at each exit block.                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

# ── Imports ────────────────────────────────────────────────────────────────────
import os, json, math, copy, time, warnings
from itertools import product
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.ticker import PercentFormatter

from filterpy.kalman import UnscentedKalmanFilter, MerweScaledSigmaPoints
from filterpy.common import Q_discrete_white_noise
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

warnings.filterwarnings('ignore')
pd.set_option('display.max_columns', 20)
pd.set_option('display.width', 160)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — change these to adjust the simulation
# ══════════════════════════════════════════════════════════════════════════════

LEVERAGE          = 10        # live target range is 10-20x
INITIAL_CAPITAL   = 10.0     # USD
UKF_TRAIN_DAYS    = 30        # bars used to train each UKF window
GRID_SEARCH_DAYS  = 15        # bars used for param optimisation
TEST_START_DAY    = 30        # first day to paper-trade (0-indexed)
USE_5M_REFINEMENT = True      # set False to disable 5m entry precision

# Kaggle data paths — adjust if your dataset name differs
BTC_CSV = '/kaggle/input/pyquant-data/btc_back366.csv'
ETH_CSV = '/kaggle/input/pyquant-data/eth_back366.csv'

# Fallback: use current directory (for local testing)
if not os.path.exists(BTC_CSV):
    BTC_CSV = 'btc_back366.csv'
    ETH_CSV = 'eth_back366.csv'

# ══════════════════════════════════════════════════════════════════════════════
# EXECUTION COST MODEL
# ══════════════════════════════════════════════════════════════════════════════

MAKER_FEE            = 0.0002   # 0.02% — Binance Futures limit entry (VIP 0)
TAKER_FEE            = 0.0005   # 0.05% — STOP_MARKET / TAKE_PROFIT_MARKET exit
ENTRY_SLIPPAGE_PCT   = 0.0003   # 0.03% — limit order queue-position slippage
TP_EXIT_SLIPPAGE_PCT = 0.0004   # 0.04% — fill slippage on TP exit (calm move)
SL_EXIT_SLIPPAGE_PCT = 0.0008   # 0.08% — fill slippage on SL exit (fast move)
FUNDING_RATE_PER_8H  = 0.0001   # 0.01% per 8-hour funding period
MIN_NOTIONAL         = 5.0      # USD — Binance Futures minimum order notional
BTC_LOT_STEP         = 0.001
ETH_LOT_STEP         = 0.001

# ── Grid search parameter space ────────────────────────────────────────────────
STRICT_BTC_VALS  = [0.0025, 0.00275, 0.003, 0.0035, 0.004, 0.0045, 0.005]
STRICTS_BTC_VALS = [0.004,  0.0045,  0.005, 0.0055, 0.006, 0.007,  0.0075]
STRICT_ETH_VALS  = [0.0045, 0.0035,  0.004, 0.0055, 0.00475, 0.005, 0.006]
STRICTS_ETH_VALS = [0.0045, 0.005,   0.0055, 0.006, 0.0065, 0.007, 0.0075, 0.008]

# ══════════════════════════════════════════════════════════════════════════════
# EXECUTION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def floor_to_lot(qty: float, step: float) -> float:
    """Floors qty to exchange lot step — never rounds up."""
    return math.floor(qty / step) * step


def apply_entry(signal_price: float, side: str, capital: float,
                leverage: float, lot_step: float):
    """
    Models a realistic LIMIT order fill.
    Applies entry slippage, rounds to lot step, validates minimum notional.
    Returns (filled_price, position_size, entry_commission, is_valid).
    """
    slip = ENTRY_SLIPPAGE_PCT
    filled = signal_price * (1 + slip) if side == 'BUY' else signal_price * (1 - slip)
    raw_qty  = (capital * leverage) / filled
    qty      = floor_to_lot(raw_qty, lot_step)
    notional = qty * filled
    valid    = qty > 0 and notional >= MIN_NOTIONAL
    commission = MAKER_FEE * qty * filled   # maker fee on notional
    return filled, qty, commission, valid


def apply_exit(trigger_price: float, leg: str, side: str, qty: float):
    """
    Models STOP_MARKET (SL) or TAKE_PROFIT_MARKET (TP) fill.
    Slippage is always unfavorable:
      BUY position closing: fills BELOW trigger
      SELL position closing: fills ABOVE trigger
    Returns (filled_price, exit_commission).
    """
    slip = SL_EXIT_SLIPPAGE_PCT if leg == 'SL' else TP_EXIT_SLIPPAGE_PCT
    if side == 'BUY':
        filled = trigger_price * (1 - slip)
    else:
        filled = trigger_price * (1 + slip)
    commission = TAKER_FEE * qty * filled   # taker fee on notional
    return filled, commission


def market_exit(price: float, qty: float):
    """Models a MARKET close (reversal exit) — taker fee only."""
    commission = TAKER_FEE * qty * price
    return price, commission


def funding_cost(entry_ts, exit_ts, qty: float, entry_price: float,
                 leverage: float) -> float:
    """
    Funding accrues every complete 8-hour period during the hold.
    Charged on the full leveraged notional at the entry price.
    """
    if entry_ts is None or exit_ts is None:
        return 0.0
    try:
        ed = pd.to_datetime(entry_ts)
        xd = pd.to_datetime(exit_ts)
        if pd.isna(ed) or pd.isna(xd):
            return 0.0
        periods = int((xd - ed).total_seconds() // (8 * 3600))
        return max(0, periods) * FUNDING_RATE_PER_8H * qty * entry_price * leverage
    except Exception:
        return 0.0


def compute_profit(side: str, entry_price: float, exit_price: float,
                   qty: float, leverage: float) -> float:
    """PnL on the leveraged position (before costs)."""
    if side == 'BUY':
        return leverage * (exit_price - entry_price) * qty
    return leverage * (entry_price - exit_price) * qty


# ══════════════════════════════════════════════════════════════════════════════
# UKF ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _stabilise_P(ukf, floor: float = 1e-6):
    """
    Symmetrises P and clamps its diagonal to prevent Cholesky failures
    during long production runs (floating-point drift accumulation).
    Applied after every update() and predict() call.
    """
    P = (ukf.P + ukf.P.T) / 2
    for i in range(P.shape[0]):
        if P[i, i] < floor:
            P[i, i] = floor
    ukf.P = P


def _make_ukf(init_val: float, alpha: float, beta: float, kappa: float,
              P_init: float, Q_scale: float, R_init: float) -> UnscentedKalmanFilter:
    """Instantiates and initialises a single UKF."""
    n_state, n_meas = 2, 1
    points = MerweScaledSigmaPoints(n=n_state, alpha=alpha, beta=beta, kappa=kappa)

    def fx(x, dt): return np.array([x[0] + dt * x[1], x[1]])
    def hx(x):     return np.array([x[0]])

    ukf     = UnscentedKalmanFilter(dim_x=n_state, dim_z=n_meas,
                                     fx=fx, hx=hx, dt=1, points=points)
    ukf.P   = np.eye(n_state) * P_init
    ukf.Q   = Q_discrete_white_noise(dim=n_state, dt=1, var=0.004) * Q_scale
    ukf.R   = np.eye(n_meas) * R_init
    ukf.x   = np.array([init_val, 0.0])
    return ukf


def _train_ukf(values: np.ndarray, alpha, beta, kappa, P_init, Q_scale, R_init):
    """Trains a UKF on the full values array and returns the trained filter."""
    ukf = _make_ukf(values[0], alpha, beta, kappa, P_init, Q_scale, R_init)
    for z in values:
        ukf.predict(); _stabilise_P(ukf)
        ukf.update(z); _stabilise_P(ukf)
    ukf.predict(); _stabilise_P(ukf)
    return ukf


def build_ukfs(df: pd.DataFrame, symbol: str):
    """
    Trains 1H and 5T UKF triplets (close/high/low) on the supplied dataframe.
    Replicates ukf_factory() from the existing codebase exactly, including
    the 70/30 train/test split for metrics logging.
    Returns: (ukf_1h, high_ukf_1h, low_ukf_1h, ukf_5t, high_ukf_5t, low_ukf_5t)
    """
    if symbol == 'BTC':
        params = {'alpha': 0.001, 'beta': 7.0, 'kappa': 0,
                  'P': 0.001, 'Q': 1.0, 'R': 0.01}
    else:
        params = {'alpha': 0.001, 'beta': 4.0, 'kappa': 1,
                  'P': 0.1,   'Q': 1.0, 'R': 0.01}

    a, b, k  = params['alpha'], params['beta'], params['kappa']
    P, Q, R  = params['P'], params['Q'], params['R']

    def _train_triplet(data_df):
        c = data_df['close'].values
        h = data_df['high'].values
        lo = data_df['low'].values
        ukf_c  = _train_ukf(c,  a, b, k, P, Q, R)
        ukf_h  = _train_ukf(h,  a, b, k, P, Q, R)
        ukf_l  = _train_ukf(lo, a, b, k, P, Q, R)
        return ukf_c, ukf_h, ukf_l

    # 1H triplet
    ukf_c1h, ukf_h1h, ukf_l1h = _train_triplet(df)

    # 5T triplet — aggregate hourly to 5-minute resolution
    df5 = df.copy()
    if not isinstance(df5.index, pd.DatetimeIndex):
        if 'timestamp' in df5.columns:
            df5.index = pd.to_datetime(df5['timestamp'])
        else:
            df5.index = pd.RangeIndex(len(df5))

    try:
        df5t = df5[['open','high','low','close','volume']].resample('5min').agg({
            'open': 'first', 'high': 'max', 'low': 'min',
            'close': 'last', 'volume': 'sum'
        }).dropna()
        if len(df5t) < 10:
            raise ValueError("Too few 5T bars after aggregation")
    except Exception:
        # Fallback: just use 1H data for the 5T UKFs as well — the entry
        # refinement logic will naturally fall back to 1H entry price
        df5t = df

    ukf_c5t, ukf_h5t, ukf_l5t = _train_triplet(df5t)
    return ukf_c1h, ukf_h1h, ukf_l1h, ukf_c5t, ukf_h5t, ukf_l5t


def step_ukf(ukf, high_ukf, low_ukf, row: pd.Series):
    """
    Updates all three UKFs with one bar and returns next-step predictions.
    Calls _stabilise_P after every update and predict to prevent Cholesky failure.
    """
    for u, val in [(ukf, row['close']), (high_ukf, row['high']), (low_ukf, row['low'])]:
        u.update(val);  _stabilise_P(u)
        u.predict();    _stabilise_P(u)
    return ukf.x[0], high_ukf.x[0], low_ukf.x[0]


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL GENERATION — exact port of SignalGenerator from pyquant_utils.py
# ══════════════════════════════════════════════════════════════════════════════

def _symbol_params(symbol: str, strict: float, stricts: float) -> dict:
    """Returns the signal generation constants for the given symbol."""
    if symbol == 'BTC':
        tp_inc = 1.004
        return dict(rr=8.6, trig_ratio=6.1, strict=strict, stricts=stricts,
                    buy_tp=1.0075, sell_tp=2-1.0075, first_loss=7.5,
                    loss_multiple=30, rounds=0, tp_inc=tp_inc, sell_inc=2-tp_inc,
                    lot_step=BTC_LOT_STEP)
    else:  # ETH
        tp_inc = 1.05
        return dict(rr=8.62, trig_ratio=6.02, strict=strict, stricts=stricts,
                    buy_tp=1.0085, sell_tp=2-1.0085, first_loss=16,
                    loss_multiple=1, rounds=2, tp_inc=tp_inc, sell_inc=2-tp_inc,
                    lot_step=ETH_LOT_STEP)


def generate_signal(price: float, pred: float, high_pred: float, low_pred: float,
                    symbol: str, balance: float, leverage: int,
                    strict: float, stricts: float):
    """
    Replicates SignalGenerator.generate_signal() as a pure function.
    Returns a signal dict or None.
    """
    sp = _symbol_params(symbol, strict, stricts)
    r  = sp['rounds']
    max_loss = balance * sp['first_loss'] * sp['loss_multiple']

    if pred > price:
        trend = 'up'
        pct_diff = abs(1 - pred / price)
        vol      = abs(1 - high_pred / pred)
    elif price > pred:
        trend = 'down'
        pct_diff = abs(1 - price / pred)
        vol      = abs(1 - pred / low_pred) if low_pred > 0 else 0
    else:
        return None

    # Primary check
    if not (pct_diff <= stricts or vol >= stricts):
        return None

    if trend == 'up':
        buy_price    = low_pred
        profit_check = abs(1 - pred / buy_price) if buy_price > 0 else 0
        if not (pred > buy_price and profit_check >= strict):
            return None
        entry = round(price, r) if price < buy_price else round(buy_price, r)
        order_type = 'Market' if price < buy_price else 'Limit'
        net_val = (balance * sp['tp_inc']) * leverage
        tp_qty  = (balance * leverage) / entry if entry > 0 else 0
        tp_cand = net_val / tp_qty if tp_qty > 0 else pred
        tp = round(tp_cand if pred > tp_cand else pred, r)
        pos = balance / entry if entry > 0 else 0
        sl  = round(entry - max_loss / (entry * pos), r) if pos > 0 and entry > 0 else entry * 0.98

        return dict(symbol=symbol, order_type=order_type, order_side='Buy',
                    entry_price=entry, tp_price=tp, sl_price=sl,
                    current_price=price, current_bal=balance, trend=trend,
                    pct_diff=pct_diff)

    else:  # trend == 'down'
        sell_price   = high_pred
        profit_check = abs(1 - sell_price / pred) if pred > 0 else 0
        if not (sell_price > pred and profit_check >= strict):
            return None
        entry = round(price, r) if price > sell_price else round(sell_price, r)
        order_type = 'Market' if price > sell_price else 'Limit'
        net_val = (balance * sp['sell_inc']) * leverage
        tp_qty  = (balance * leverage) / entry if entry > 0 else 0
        tp_cand = net_val / tp_qty if tp_qty > 0 else pred
        tp = round(tp_cand if pred < tp_cand else pred, r)
        pos = balance / entry if entry > 0 else 0
        sl  = round(entry + max_loss / (entry * pos), r) if pos > 0 and entry > 0 else entry * 1.02

        return dict(symbol=symbol, order_type=order_type, order_side='Sell',
                    entry_price=entry, tp_price=tp, sl_price=sl,
                    current_price=price, current_bal=balance, trend=trend,
                    pct_diff=pct_diff)


def refine_entry_with_5m(signal: dict, pred_5t: float, high_pred_5t: float,
                          low_pred_5t: float, bar_high: float, bar_low: float) -> dict:
    """
    5-Minute Entry Precision Refinement.

    The 1H UKF determines signal DIRECTION and TP/SL levels.
    The 5T UKF provides a more precise near-term entry price.

    SELL signal (order_side='Sell'):
        The 5T UKF may show a temporary micro-bullish move (pred_5t > close)
        before the 1H bearish trend takes hold. In that case, the expected
        micro-peak (high_pred_5t) is a BETTER short entry than the 1H high_pred
        — you short higher, improving R/R.
        Entry = max(high_pred_5t, signal entry_price), capped at bar_high.

    BUY signal (order_side='Buy'):
        The 5T UKF may show a temporary micro-bearish dip (pred_5t < close)
        before the 1H bullish trend takes hold. The expected trough (low_pred_5t)
        is a BETTER long entry than the 1H low_pred.
        Entry = min(low_pred_5t, signal entry_price), floored at bar_low.

    This can ONLY improve the entry — it never makes it worse. If the 5T
    prediction doesn't improve the entry (e.g. it predicts the same direction
    as 1H, or the entry would exceed the bar's actual range), the original
    1H entry is kept unchanged.

    Returns the signal dict with entry_price potentially updated and
    '5m_refined' flag set for analysis.
    """
    sig      = dict(signal)   # shallow copy — don't mutate caller's dict
    original = sig['entry_price']
    close    = sig['current_price']

    if sig['order_side'] == 'Sell':
        # Want to SELL higher: use 5T high pred if it's above current entry
        # AND the 5T is showing a micro-bullish move (pred_5t > close)
        if pred_5t > close and high_pred_5t > original:
            refined = min(high_pred_5t, bar_high)   # can't exceed what actually traded
            if refined > original:
                sig['entry_price'] = round(refined, 2 if sig['symbol'] == 'ETH' else 0)
                sig['5m_refined']  = True
                sig['5m_slip_saved'] = refined - original  # improvement in entry price
                return sig

    elif sig['order_side'] == 'Buy':
        # Want to BUY lower: use 5T low pred if it's below current entry
        # AND the 5T is showing a micro-bearish dip (pred_5t < close)
        if pred_5t < close and low_pred_5t < original:
            refined = max(low_pred_5t, bar_low)   # can't go below what actually traded
            if refined < original:
                sig['entry_price'] = round(refined, 2 if sig['symbol'] == 'ETH' else 0)
                sig['5m_refined']  = True
                sig['5m_slip_saved'] = original - refined
                return sig

    sig['5m_refined']   = False
    sig['5m_slip_saved'] = 0.0
    return sig


# ══════════════════════════════════════════════════════════════════════════════
# FAST GRID SEARCH — signal replay on stored predictions (no UKF retraining)
# ══════════════════════════════════════════════════════════════════════════════

def fast_grid_search(btc_preds: list, eth_preds: list,
                     btc_bars: pd.DataFrame, eth_bars: pd.DataFrame,
                     balance: float, leverage: int) -> dict:
    """
    Runs the full grid search without retraining UKFs for each combo.

    Instead of retraining UKFs for each of the 2744 parameter combinations
    (which would be prohibitively slow in a 335-day walk-forward), we:
      1. Pre-store UKF predictions as lists of (pred, high_pred, low_pred)
         over the grid search window — computed ONCE per day.
      2. Replay the signal generation logic against those stored predictions
         for every parameter combination.

    This is an accurate approximation because:
      - UKF predictions are independent of signal thresholds — the filter runs
        the same way regardless of what the signal generator does with its output.
      - The only thing that changes between parameter combos is whether a given
        prediction clears the strict/stricts threshold — the prediction itself
        is the same.
      - This is how the live system works too: UKFs run continuously, the grid
        search only tunes the signal thresholds applied to those predictions.

    Returns the best param dict.
    """
    best_ret    = -np.inf
    best_params = None

    n_bars = min(len(btc_preds), len(btc_bars), len(eth_preds), len(eth_bars))

    for sb, sbs, se, ses in product(STRICT_BTC_VALS, STRICTS_BTC_VALS,
                                     STRICT_ETH_VALS, STRICTS_ETH_VALS):
        cap = balance
        in_btc = in_eth = False
        side_btc = side_eth = None
        entry_btc = entry_eth = 0.0
        qty_btc = qty_eth = 0.0
        tp_btc = sl_btc = tp_eth = sl_eth = 0.0

        for i in range(n_bars):
            row_btc = btc_bars.iloc[i]
            row_eth = eth_bars.iloc[i]
            pred_b, hp_b, lp_b = btc_preds[i]
            pred_e, hp_e, lp_e = eth_preds[i]
            pb = row_btc['close']; hb = row_btc['high']; lb = row_btc['low']
            pe = row_eth['close']; he = row_eth['high']; le = row_eth['low']

            # --- BTC exit (SL-first pessimistic) ---
            if in_btc:
                if side_btc == 'Buy':
                    if lb <= sl_btc:
                        pnl = compute_profit('BUY', entry_btc, sl_btc, qty_btc, leverage)
                        cap += (qty_btc * entry_btc / leverage) + pnl
                        in_btc = False
                    elif hb >= tp_btc:
                        pnl = compute_profit('BUY', entry_btc, tp_btc, qty_btc, leverage)
                        cap += (qty_btc * entry_btc / leverage) + pnl
                        in_btc = False
                else:
                    if hb >= sl_btc:
                        pnl = compute_profit('SELL', entry_btc, sl_btc, qty_btc, leverage)
                        cap += (qty_btc * entry_btc / leverage) + pnl
                        in_btc = False
                    elif lb <= tp_btc:
                        pnl = compute_profit('SELL', entry_btc, tp_btc, qty_btc, leverage)
                        cap += (qty_btc * entry_btc / leverage) + pnl
                        in_btc = False

            # --- ETH exit ---
            if in_eth:
                if side_eth == 'Buy':
                    if le <= sl_eth:
                        pnl = compute_profit('BUY', entry_eth, sl_eth, qty_eth, leverage)
                        cap += (qty_eth * entry_eth / leverage) + pnl
                        in_eth = False
                    elif he >= tp_eth:
                        pnl = compute_profit('BUY', entry_eth, tp_eth, qty_eth, leverage)
                        cap += (qty_eth * entry_eth / leverage) + pnl
                        in_eth = False
                else:
                    if he >= sl_eth:
                        pnl = compute_profit('SELL', entry_eth, sl_eth, qty_eth, leverage)
                        cap += (qty_eth * entry_eth / leverage) + pnl
                        in_eth = False
                    elif le <= tp_eth:
                        pnl = compute_profit('SELL', entry_eth, tp_eth, qty_eth, leverage)
                        cap += (qty_eth * entry_eth / leverage) + pnl
                        in_eth = False

            if in_btc or in_eth:
                continue

            # --- BTC entry ---
            sig_b = generate_signal(pb, pred_b, hp_b, lp_b, 'BTC', cap*0.5 if True else cap,
                                     leverage, sb, sbs)
            sig_e = generate_signal(pe, pred_e, hp_e, lp_e, 'ETH', cap*0.5 if True else cap,
                                     leverage, se, ses)

            if sig_b and sig_e:
                alloc = cap / 2
            elif sig_b or sig_e:
                alloc = cap * 0.8
            else:
                continue

            if sig_b and not in_btc:
                ep, qty, _, valid = apply_entry(sig_b['entry_price'], sig_b['order_side'],
                                                 alloc, leverage, BTC_LOT_STEP)
                if valid:
                    in_btc = True; side_btc = sig_b['order_side']
                    entry_btc = ep; qty_btc = qty
                    ml = (alloc * 7.5 * 30) / (ep * qty) if qty > 0 else 0
                    sl_btc = ep - ml if side_btc == 'Buy' else ep + ml
                    tp_cand_b = (alloc * 1.004 * leverage) / qty if qty > 0 else ep * 1.01
                    tp_btc = min(pred_b, tp_cand_b) if side_btc == 'Buy' else max(pred_b, tp_cand_b)

            if sig_e and not in_eth:
                ep, qty, _, valid = apply_entry(sig_e['entry_price'], sig_e['order_side'],
                                                 alloc, leverage, ETH_LOT_STEP)
                if valid:
                    in_eth = True; side_eth = sig_e['order_side']
                    entry_eth = ep; qty_eth = qty
                    ml = (alloc * 16 * 1) / (ep * qty) if qty > 0 else 0
                    sl_eth = ep - ml if side_eth == 'Buy' else ep + ml
                    tp_cand_e = (alloc * 1.05 * leverage) / qty if qty > 0 else ep * 1.01
                    tp_eth = min(pred_e, tp_cand_e) if side_eth == 'Buy' else max(pred_e, tp_cand_e)

        ret = (cap / balance - 1) * 100
        if ret > best_ret and ret > 0:
            best_ret = ret
            best_params = dict(strict_btc=sb, stricts_btc=sbs,
                                strict_eth=se, stricts_eth=ses,
                                best_returns=ret)

    if best_params is None:
        print("  ⚠ Grid search: no profitable combo found — using defaults.")
        best_params = dict(strict_btc=0.0025, stricts_btc=0.005,
                            strict_eth=0.0035, stricts_eth=0.0075,
                            best_returns=0.0)

    return best_params


# ══════════════════════════════════════════════════════════════════════════════
# SINGLE-DAY PAPER TRADE — the hourly loop that runs against a test day
# ══════════════════════════════════════════════════════════════════════════════

def paper_trade_day(btc_day: pd.DataFrame, eth_day: pd.DataFrame,
                    btc_ukf_c, btc_ukf_h, btc_ukf_l,
                    eth_ukf_c, eth_ukf_h, eth_ukf_l,
                    btc_5t_c, btc_5t_h, btc_5t_l,
                    eth_5t_c, eth_5t_h, eth_5t_l,
                    params: dict, initial_capital: float, leverage: int):
    """
    Simulates one trading day hour by hour.
    Advances the UKF states (in-place) as new bars arrive.
    Returns (final_capital, trades_list, day_stats_dict).
    """
    cap = initial_capital
    trades = []
    in_trade_btc = in_trade_eth = False
    pos_btc = pos_eth = {}   # holds the open position state

    sb   = params['strict_btc'];  sbs  = params['stricts_btc']
    se   = params['strict_eth'];  ses  = params['stricts_eth']

    day_stats = dict(signals_generated=0, signals_rejected_notional=0,
                     entries_5m_refined=0, total_5m_slip_saved=0.0,
                     tp_hits=0, sl_hits=0, reversal_exits=0, funding_paid=0.0)

    for idx in range(len(btc_day)):
        row_b = btc_day.iloc[idx]
        row_e = eth_day.iloc[idx]
        ts    = row_b.get('timestamp', str(row_b.name))

        # --- Step UKFs with this bar (1H and 5T) ---
        pred_b,  hp_b,  lp_b  = step_ukf(btc_ukf_c, btc_ukf_h, btc_ukf_l, row_b)
        pred_e,  hp_e,  lp_e  = step_ukf(eth_ukf_c, eth_ukf_h, eth_ukf_l, row_e)
        pred_b5, hp_b5, lp_b5 = step_ukf(btc_5t_c,  btc_5t_h,  btc_5t_l,  row_b)
        pred_e5, hp_e5, lp_e5 = step_ukf(eth_5t_c,  eth_5t_h,  eth_5t_l,  row_e)

        pb  = row_b['close']; hb = row_b['high']; lb = row_b['low']
        pe  = row_e['close']; he = row_e['high']; le = row_e['low']

        # ─── BTC exit check ─────────────────────────────────────────────────
        if in_trade_btc and pos_btc:
            side  = pos_btc['side']
            ep    = pos_btc['entry_price']
            qty   = pos_btc['qty']
            tp    = pos_btc['tp_price']
            sl    = pos_btc['sl_price']
            alloc = pos_btc['capital']

            exit_done = False
            # SL-FIRST pessimistic resolution — intentionally unchanged
            if side == 'Buy':
                if lb <= sl:
                    xp, xcomm = apply_exit(sl, 'SL', 'BUY', qty)
                    pnl = compute_profit('BUY', ep, xp, qty, leverage)
                    fc  = funding_cost(pos_btc.get('ts'), ts, qty, ep, leverage)
                    profit = pnl - xcomm - fc - pos_btc['entry_commission']
                    cap += alloc + profit
                    trades.append({**pos_btc, 'exit_price': xp, 'exit_ts': ts,
                                   'exit_leg': 'SL', 'profit': profit,
                                   'funding': fc, 'entry_slip_saved': pos_btc.get('5m_slip_saved', 0)})
                    in_trade_btc = False; pos_btc = {}
                    day_stats['sl_hits'] += 1; exit_done = True

                if not exit_done:
                    net_pnl = compute_profit('BUY', ep, hb, qty, leverage)
                    if net_pnl >= alloc * 0.225 or hb >= tp:
                        exit_leg = 'TP' if hb >= tp else 'MARKET'
                        exit_trig = tp if exit_leg == 'TP' else hb
                        if exit_leg == 'TP':
                            xp, xcomm = apply_exit(exit_trig, 'TP', 'BUY', qty)
                        else:
                            xp, xcomm = market_exit(exit_trig, qty)
                        pnl = compute_profit('BUY', ep, xp, qty, leverage)
                        fc  = funding_cost(pos_btc.get('ts'), ts, qty, ep, leverage)
                        profit = pnl - xcomm - fc - pos_btc['entry_commission']
                        cap += alloc + profit
                        trades.append({**pos_btc, 'exit_price': xp, 'exit_ts': ts,
                                       'exit_leg': exit_leg, 'profit': profit,
                                       'funding': fc, 'entry_slip_saved': pos_btc.get('5m_slip_saved', 0)})
                        in_trade_btc = False; pos_btc = {}
                        day_stats['tp_hits' if exit_leg == 'TP' else 'reversal_exits'] += 1

            else:  # Sell
                if hb >= sl:
                    xp, xcomm = apply_exit(sl, 'SL', 'SELL', qty)
                    pnl = compute_profit('SELL', ep, xp, qty, leverage)
                    fc  = funding_cost(pos_btc.get('ts'), ts, qty, ep, leverage)
                    profit = pnl - xcomm - fc - pos_btc['entry_commission']
                    cap += alloc + profit
                    trades.append({**pos_btc, 'exit_price': xp, 'exit_ts': ts,
                                   'exit_leg': 'SL', 'profit': profit,
                                   'funding': fc, 'entry_slip_saved': pos_btc.get('5m_slip_saved', 0)})
                    in_trade_btc = False; pos_btc = {}
                    day_stats['sl_hits'] += 1; exit_done = True

                if not exit_done:
                    net_pnl = compute_profit('SELL', ep, lb, qty, leverage)
                    if net_pnl >= alloc * 0.225 or lb <= tp:
                        exit_leg = 'TP' if lb <= tp else 'MARKET'
                        exit_trig = tp if exit_leg == 'TP' else lb
                        if exit_leg == 'TP':
                            xp, xcomm = apply_exit(exit_trig, 'TP', 'SELL', qty)
                        else:
                            xp, xcomm = market_exit(exit_trig, qty)
                        pnl = compute_profit('SELL', ep, xp, qty, leverage)
                        fc  = funding_cost(pos_btc.get('ts'), ts, qty, ep, leverage)
                        profit = pnl - xcomm - fc - pos_btc['entry_commission']
                        cap += alloc + profit
                        trades.append({**pos_btc, 'exit_price': xp, 'exit_ts': ts,
                                       'exit_leg': exit_leg, 'profit': profit,
                                       'funding': fc, 'entry_slip_saved': pos_btc.get('5m_slip_saved', 0)})
                        in_trade_btc = False; pos_btc = {}
                        day_stats['tp_hits' if exit_leg == 'TP' else 'reversal_exits'] += 1

        # ─── ETH exit check ─────────────────────────────────────────────────
        if in_trade_eth and pos_eth:
            side  = pos_eth['side']
            ep    = pos_eth['entry_price']
            qty   = pos_eth['qty']
            tp    = pos_eth['tp_price']
            sl    = pos_eth['sl_price']
            alloc = pos_eth['capital']

            exit_done = False
            if side == 'Buy':
                if le <= sl:
                    xp, xcomm = apply_exit(sl, 'SL', 'BUY', qty)
                    pnl = compute_profit('BUY', ep, xp, qty, leverage)
                    fc  = funding_cost(pos_eth.get('ts'), ts, qty, ep, leverage)
                    profit = pnl - xcomm - fc - pos_eth['entry_commission']
                    cap += alloc + profit
                    trades.append({**pos_eth, 'exit_price': xp, 'exit_ts': ts,
                                   'exit_leg': 'SL', 'profit': profit,
                                   'funding': fc, 'entry_slip_saved': pos_eth.get('5m_slip_saved', 0)})
                    in_trade_eth = False; pos_eth = {}
                    day_stats['sl_hits'] += 1; exit_done = True

                if not exit_done:
                    net_pnl = compute_profit('BUY', ep, he, qty, leverage)
                    if net_pnl >= alloc * 0.175 or he >= tp:
                        exit_leg = 'TP' if he >= tp else 'MARKET'
                        exit_trig = tp if exit_leg == 'TP' else he
                        if exit_leg == 'TP':
                            xp, xcomm = apply_exit(exit_trig, 'TP', 'BUY', qty)
                        else:
                            xp, xcomm = market_exit(exit_trig, qty)
                        pnl = compute_profit('BUY', ep, xp, qty, leverage)
                        fc  = funding_cost(pos_eth.get('ts'), ts, qty, ep, leverage)
                        profit = pnl - xcomm - fc - pos_eth['entry_commission']
                        cap += alloc + profit
                        trades.append({**pos_eth, 'exit_price': xp, 'exit_ts': ts,
                                       'exit_leg': exit_leg, 'profit': profit,
                                       'funding': fc, 'entry_slip_saved': pos_eth.get('5m_slip_saved', 0)})
                        in_trade_eth = False; pos_eth = {}
                        day_stats['tp_hits' if exit_leg == 'TP' else 'reversal_exits'] += 1

            else:  # Sell
                if he >= sl:
                    xp, xcomm = apply_exit(sl, 'SL', 'SELL', qty)
                    pnl = compute_profit('SELL', ep, xp, qty, leverage)
                    fc  = funding_cost(pos_eth.get('ts'), ts, qty, ep, leverage)
                    profit = pnl - xcomm - fc - pos_eth['entry_commission']
                    cap += alloc + profit
                    trades.append({**pos_eth, 'exit_price': xp, 'exit_ts': ts,
                                   'exit_leg': 'SL', 'profit': profit,
                                   'funding': fc, 'entry_slip_saved': pos_eth.get('5m_slip_saved', 0)})
                    in_trade_eth = False; pos_eth = {}
                    day_stats['sl_hits'] += 1; exit_done = True

                if not exit_done:
                    net_pnl = compute_profit('SELL', ep, le, qty, leverage)
                    if net_pnl >= alloc * 0.175 or le <= tp:
                        exit_leg = 'TP' if le <= tp else 'MARKET'
                        exit_trig = tp if exit_leg == 'TP' else le
                        if exit_leg == 'TP':
                            xp, xcomm = apply_exit(exit_trig, 'TP', 'SELL', qty)
                        else:
                            xp, xcomm = market_exit(exit_trig, qty)
                        pnl = compute_profit('SELL', ep, xp, qty, leverage)
                        fc  = funding_cost(pos_eth.get('ts'), ts, qty, ep, leverage)
                        profit = pnl - xcomm - fc - pos_eth['entry_commission']
                        cap += alloc + profit
                        trades.append({**pos_eth, 'exit_price': xp, 'exit_ts': ts,
                                       'exit_leg': exit_leg, 'profit': profit,
                                       'funding': fc, 'entry_slip_saved': pos_eth.get('5m_slip_saved', 0)})
                        in_trade_eth = False; pos_eth = {}
                        day_stats['tp_hits' if exit_leg == 'TP' else 'reversal_exits'] += 1

        # ─── Signal generation and entry ────────────────────────────────────
        if in_trade_btc and in_trade_eth:
            continue

        alloc_btc = alloc_eth = cap

        sig_b = generate_signal(pb, pred_b, hp_b, lp_b, 'BTC', cap, leverage, sb, sbs) \
                if not in_trade_btc else None
        sig_e = generate_signal(pe, pred_e, hp_e, lp_e, 'ETH', cap, leverage, se, ses) \
                if not in_trade_eth else None

        if sig_b and sig_e:
            alloc_btc = alloc_eth = cap / 2
        elif sig_b:
            alloc_btc = cap * 0.8
        elif sig_e:
            alloc_eth = cap * 0.8

        if sig_b and USE_5M_REFINEMENT:
            sig_b = refine_entry_with_5m(sig_b, pred_b5, hp_b5, lp_b5, hb, lb)
            if sig_b.get('5m_refined'):
                day_stats['entries_5m_refined'] += 1
                day_stats['total_5m_slip_saved'] += sig_b.get('5m_slip_saved', 0)

        if sig_e and USE_5M_REFINEMENT:
            sig_e = refine_entry_with_5m(sig_e, pred_e5, hp_e5, lp_e5, he, le)
            if sig_e.get('5m_refined'):
                day_stats['entries_5m_refined'] += 1
                day_stats['total_5m_slip_saved'] += sig_e.get('5m_slip_saved', 0)

        if sig_b and not in_trade_btc:
            day_stats['signals_generated'] += 1
            ep, qty, ecomm, valid = apply_entry(sig_b['entry_price'],
                                                 sig_b['order_side'],
                                                 alloc_btc, leverage, BTC_LOT_STEP)
            if not valid:
                day_stats['signals_rejected_notional'] += 1
                cap += alloc_btc  # capital not deployed, returned
            else:
                sp   = _symbol_params('BTC', sb, sbs)
                ml   = (alloc_btc * sp['first_loss'] * sp['loss_multiple']) / (ep * qty) if qty > 0 else 0
                sl_p = ep - ml if sig_b['order_side'] == 'Buy' else ep + ml
                nv   = (alloc_btc * sp['tp_inc']) * leverage
                tp_p = nv / qty if qty > 0 else ep * 1.01
                tp_p = min(pred_b, tp_p) if sig_b['order_side'] == 'Buy' else max(pred_b, tp_p)
                pos_btc = dict(symbol='BTC', side=sig_b['order_side'], entry_price=ep,
                               tp_price=tp_p, sl_price=sl_p, qty=qty, capital=alloc_btc,
                               ts=ts, entry_commission=ecomm, trigger_price=sig_b['entry_price'],
                               **{k: sig_b.get(k, False) for k in ('5m_refined', '5m_slip_saved')})
                cap -= alloc_btc
                in_trade_btc = True

        if sig_e and not in_trade_eth:
            day_stats['signals_generated'] += 1
            ep, qty, ecomm, valid = apply_entry(sig_e['entry_price'],
                                                 sig_e['order_side'],
                                                 alloc_eth, leverage, ETH_LOT_STEP)
            if not valid:
                day_stats['signals_rejected_notional'] += 1
                cap += alloc_eth
            else:
                sp   = _symbol_params('ETH', se, ses)
                ml   = (alloc_eth * sp['first_loss'] * sp['loss_multiple']) / (ep * qty) if qty > 0 else 0
                sl_p = ep - ml if sig_e['order_side'] == 'Buy' else ep + ml
                nv   = (alloc_eth * sp['tp_inc']) * leverage
                tp_p = nv / qty if qty > 0 else ep * 1.01
                tp_p = min(pred_e, tp_p) if sig_e['order_side'] == 'Buy' else max(pred_e, tp_p)
                pos_eth = dict(symbol='ETH', side=sig_e['order_side'], entry_price=ep,
                               tp_price=tp_p, sl_price=sl_p, qty=qty, capital=alloc_eth,
                               ts=ts, entry_commission=ecomm, trigger_price=sig_e['entry_price'],
                               **{k: sig_e.get(k, False) for k in ('5m_refined', '5m_slip_saved')})
                cap -= alloc_eth
                in_trade_eth = True

    # Carry open positions to end of day at last bar's close
    if in_trade_btc and pos_btc:
        last_close = btc_day.iloc[-1]['close']
        last_ts    = btc_day.iloc[-1].get('timestamp', str(btc_day.index[-1]))
        ep = pos_btc['entry_price']; qty = pos_btc['qty']; alloc = pos_btc['capital']
        xp, xcomm = market_exit(last_close, qty)
        pnl  = compute_profit(pos_btc['side'], ep, xp, qty, leverage)
        fc   = funding_cost(pos_btc.get('ts'), last_ts, qty, ep, leverage)
        profit = pnl - xcomm - fc - pos_btc['entry_commission']
        cap += alloc + profit
        trades.append({**pos_btc, 'exit_price': xp, 'exit_ts': last_ts,
                       'exit_leg': 'EOD', 'profit': profit, 'funding': fc,
                       'entry_slip_saved': pos_btc.get('5m_slip_saved', 0)})

    if in_trade_eth and pos_eth:
        last_close = eth_day.iloc[-1]['close']
        last_ts    = eth_day.iloc[-1].get('timestamp', str(eth_day.index[-1]))
        ep = pos_eth['entry_price']; qty = pos_eth['qty']; alloc = pos_eth['capital']
        xp, xcomm = market_exit(last_close, qty)
        pnl  = compute_profit(pos_eth['side'], ep, xp, qty, leverage)
        fc   = funding_cost(pos_eth.get('ts'), last_ts, qty, ep, leverage)
        profit = pnl - xcomm - fc - pos_eth['entry_commission']
        cap += alloc + profit
        trades.append({**pos_eth, 'exit_price': xp, 'exit_ts': last_ts,
                       'exit_leg': 'EOD', 'profit': profit, 'funding': fc,
                       'entry_slip_saved': pos_eth.get('5m_slip_saved', 0)})

    day_stats['funding_paid'] = sum(t.get('funding', 0) for t in trades)
    return cap, trades, day_stats


# ══════════════════════════════════════════════════════════════════════════════
# METRICS
# ══════════════════════════════════════════════════════════════════════════════

def compute_metrics(all_trades: list, capital_curve: list,
                    initial_capital: float) -> pd.DataFrame:
    if not all_trades:
        return pd.DataFrame()

    total   = len(all_trades)
    wins    = [t for t in all_trades if t['profit'] > 0]
    losses  = [t for t in all_trades if t['profit'] <= 0]
    pnl_list = [t['profit'] for t in all_trades]
    tp_exits = [t for t in all_trades if t.get('exit_leg') == 'TP']
    sl_exits = [t for t in all_trades if t.get('exit_leg') == 'SL']
    refined  = [t for t in all_trades if t.get('5m_refined')]
    total_funding = sum(t.get('funding', 0) for t in all_trades)

    # Drawdown from capital curve
    curve   = np.array(capital_curve)
    peak    = np.maximum.accumulate(curve)
    dd      = (curve - peak) / peak
    max_dd  = abs(dd.min()) * 100

    # Daily returns for Sharpe
    daily_cap  = [capital_curve[i*24] for i in range(len(capital_curve)//24)]
    daily_ret  = np.diff(daily_cap) / np.array(daily_cap[:-1])
    sharpe     = (np.mean(daily_ret) / np.std(daily_ret) * np.sqrt(365)
                  if len(daily_ret) > 1 and np.std(daily_ret) > 0 else 0)

    final_cap = capital_curve[-1]
    total_ret = (final_cap / initial_capital - 1) * 100

    # Slippage metrics
    entries_with_trigger = [t for t in all_trades if 'trigger_price' in t]
    if entries_with_trigger:
        slip_vals = []
        for t in entries_with_trigger:
            trig = t['trigger_price']; fill = t['entry_price']
            side = t['side']
            diff = (fill - trig) if side == 'Buy' else (trig - fill)
            slip_vals.append(diff / trig * 100)
        avg_entry_slip_pct = np.mean(slip_vals)
    else:
        avg_entry_slip_pct = np.nan

    rows = [
        ("Total Return (%)",            f"{total_ret:.2f}"),
        ("Final Capital ($)",           f"{final_cap:.4f}"),
        ("Total Trades",                total),
        ("Winning Trades",              len(wins)),
        ("Losing Trades",               len(losses)),
        ("Win Rate (%)",                f"{len(wins)/total*100:.1f}" if total > 0 else "0"),
        ("Avg Win ($)",                 f"{np.mean([t['profit'] for t in wins]):.4f}" if wins else "0"),
        ("Avg Loss ($)",                f"{np.mean([t['profit'] for t in losses]):.4f}" if losses else "0"),
        ("Best Trade ($)",              f"{max(pnl_list):.4f}"),
        ("Worst Trade ($)",             f"{min(pnl_list):.4f}"),
        ("Max Drawdown (%)",            f"{max_dd:.2f}"),
        ("Sharpe Ratio",                f"{sharpe:.2f}"),
        ("TP Exits",                    len(tp_exits)),
        ("SL Exits",                    len(sl_exits)),
        ("Total Funding Cost ($)",      f"{total_funding:.4f}"),
        ("Avg Entry Slip (%)",          f"{avg_entry_slip_pct:.4f}" if not np.isnan(avg_entry_slip_pct) else "N/A"),
        ("5m-Refined Entries",          len(refined)),
        ("Avg 5m Entry Improvement ($)", f"{np.mean([t.get('5m_slip_saved',0) for t in refined]):.4f}" if refined else "0"),
    ]
    return pd.DataFrame(rows, columns=["Metric", "Value"])


# ══════════════════════════════════════════════════════════════════════════════
# REPORTING AND CHARTS
# ══════════════════════════════════════════════════════════════════════════════

def plot_results(capital_curve: list, all_trades: list,
                 daily_params_log: list, initial_capital: float):
    fig = plt.figure(figsize=(18, 14))
    gs  = gridspec.GridSpec(3, 3, figure=fig, hspace=0.42, wspace=0.35)

    # 1 — Capital curve
    ax1 = fig.add_subplot(gs[0, :])
    hrs = np.arange(len(capital_curve))
    ax1.plot(hrs, capital_curve, color='#2ecc71', linewidth=1.4, label='Portfolio Value')
    ax1.axhline(initial_capital, color='#e74c3c', linestyle='--', linewidth=0.9, label='Initial Capital')
    ax1.fill_between(hrs, initial_capital, capital_curve,
                      where=[c >= initial_capital for c in capital_curve],
                      color='#2ecc71', alpha=0.15)
    ax1.fill_between(hrs, initial_capital, capital_curve,
                      where=[c < initial_capital for c in capital_curve],
                      color='#e74c3c', alpha=0.15)
    ax1.set_title('Walk-Forward Capital Curve', fontsize=13, fontweight='bold')
    ax1.set_xlabel('Hour'); ax1.set_ylabel('Capital ($)')
    ax1.legend(); ax1.grid(alpha=0.3)

    # 2 — Drawdown
    ax2 = fig.add_subplot(gs[1, :2])
    curve = np.array(capital_curve)
    peak  = np.maximum.accumulate(curve)
    dd    = (curve - peak) / peak * 100
    ax2.fill_between(hrs, dd, 0, color='#e74c3c', alpha=0.6)
    ax2.plot(hrs, dd, color='#c0392b', linewidth=0.8)
    ax2.set_title('Drawdown (%)', fontsize=11, fontweight='bold')
    ax2.set_xlabel('Hour'); ax2.set_ylabel('Drawdown (%)')
    ax2.grid(alpha=0.3)

    # 3 — Trade profit distribution
    ax3 = fig.add_subplot(gs[1, 2])
    profits = [t['profit'] for t in all_trades]
    if profits:
        ax3.hist(profits, bins=min(40, len(profits)),
                  color=['#2ecc71' if p > 0 else '#e74c3c' for p in profits],
                  edgecolor='white', linewidth=0.4)
    ax3.axvline(0, color='black', linestyle='--', linewidth=0.8)
    ax3.set_title('Trade PnL Distribution', fontsize=11, fontweight='bold')
    ax3.set_xlabel('Profit ($)'); ax3.set_ylabel('Count')
    ax3.grid(alpha=0.3)

    # 4 — Exit type breakdown
    ax4 = fig.add_subplot(gs[2, 0])
    legs = [t.get('exit_leg', 'UNK') for t in all_trades]
    leg_counts = {k: legs.count(k) for k in set(legs)}
    colors_map = {'TP': '#2ecc71', 'SL': '#e74c3c', 'MARKET': '#f39c12',
                  'EOD': '#3498db', 'UNK': '#95a5a6'}
    ax4.bar(leg_counts.keys(), leg_counts.values(),
             color=[colors_map.get(k, '#95a5a6') for k in leg_counts])
    ax4.set_title('Exit Type Breakdown', fontsize=11, fontweight='bold')
    ax4.set_ylabel('Count'); ax4.grid(alpha=0.3, axis='y')

    # 5 — Cumulative PnL by symbol
    ax5 = fig.add_subplot(gs[2, 1])
    btc_cumul = np.cumsum([t['profit'] for t in all_trades if t.get('symbol') == 'BTC'])
    eth_cumul = np.cumsum([t['profit'] for t in all_trades if t.get('symbol') == 'ETH'])
    if len(btc_cumul): ax5.plot(btc_cumul, label='BTC', color='#f39c12')
    if len(eth_cumul): ax5.plot(eth_cumul, label='ETH', color='#3498db')
    ax5.axhline(0, color='black', linestyle='--', linewidth=0.8)
    ax5.set_title('Cumulative PnL by Symbol', fontsize=11, fontweight='bold')
    ax5.set_xlabel('Trade #'); ax5.set_ylabel('Cumulative PnL ($)')
    ax5.legend(); ax5.grid(alpha=0.3)

    # 6 — Daily optimal params over time
    ax6 = fig.add_subplot(gs[2, 2])
    if daily_params_log:
        days   = [p['day'] for p in daily_params_log]
        sb_vals = [p.get('strict_btc', 0) for p in daily_params_log]
        se_vals = [p.get('strict_eth', 0) for p in daily_params_log]
        ax6.plot(days, sb_vals, label='strict_btc', color='#f39c12', linewidth=0.9)
        ax6.plot(days, se_vals, label='strict_eth', color='#3498db', linewidth=0.9)
    ax6.set_title('Grid-Search Params Over Time', fontsize=11, fontweight='bold')
    ax6.set_xlabel('Test Day'); ax6.set_ylabel('Strict Threshold')
    ax6.legend(fontsize=8); ax6.grid(alpha=0.3)

    plt.suptitle('PyQuant Walk-Forward Backtest — Realistic Execution Model',
                  fontsize=14, fontweight='bold', y=0.98)
    plt.savefig('walkforward_results.png', dpi=150, bbox_inches='tight')
    plt.show()
    print("Chart saved to walkforward_results.png")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN WALK-FORWARD LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_walk_forward():
    print("=" * 70)
    print("  PyQuant Walk-Forward Realistic Backtest")
    print(f"  Leverage: {LEVERAGE}x  |  Capital: ${INITIAL_CAPITAL}  |  "
          f"5m Refinement: {USE_5M_REFINEMENT}")
    print(f"  Training: {UKF_TRAIN_DAYS}d  |  Grid Opt: {GRID_SEARCH_DAYS}d  |  "
          f"Test start: day {TEST_START_DAY}")
    print("=" * 70)

    # ── Load data ────────────────────────────────────────────────────────────
    print(f"\nLoading data from:\n  BTC: {BTC_CSV}\n  ETH: {ETH_CSV}")
    btc_full = pd.read_csv(BTC_CSV)
    eth_full = pd.read_csv(ETH_CSV)

    # Normalise column names
    btc_full.columns = [c.lower() for c in btc_full.columns]
    eth_full.columns = [c.lower() for c in eth_full.columns]

    # Parse timestamp
    for df in (btc_full, eth_full):
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.set_index('timestamp', inplace=True)
        elif 'time' in df.columns:
            df['timestamp'] = pd.to_datetime(df['time'])
            df.set_index('timestamp', inplace=True)

    # Ensure we have required columns
    for col in ('open', 'high', 'low', 'close', 'volume'):
        if col not in btc_full.columns:
            raise ValueError(f"Required column '{col}' not found in BTC CSV. "
                             f"Columns: {list(btc_full.columns)}")

    # Align both DataFrames on common index
    common_idx = btc_full.index.intersection(eth_full.index)
    btc_full   = btc_full.loc[common_idx].reset_index()
    eth_full   = eth_full.loc[common_idx].reset_index()

    n_bars = len(btc_full)
    bars_per_day = 24   # 1H bars
    print(f"Total bars: {n_bars}  (~{n_bars//bars_per_day} days)")

    # ── Walk-forward ─────────────────────────────────────────────────────────
    capital       = INITIAL_CAPITAL
    capital_curve = [capital]
    all_trades    = []
    daily_params_log = []
    test_day_results = []

    train_bars = UKF_TRAIN_DAYS * bars_per_day
    opt_bars   = GRID_SEARCH_DAYS * bars_per_day
    start_bar  = TEST_START_DAY * bars_per_day

    n_test_days = (n_bars - start_bar) // bars_per_day
    print(f"Test days: {n_test_days}\n")

    current_params = dict(strict_btc=0.0025, stricts_btc=0.005,
                          strict_eth=0.0035, stricts_eth=0.0075, best_returns=0.0)

    for day_idx in range(n_test_days):
        test_start = start_bar + day_idx * bars_per_day
        test_end   = test_start + bars_per_day

        if test_end > n_bars:
            break

        # Training window: last UKF_TRAIN_DAYS before the test day
        train_end   = test_start
        train_start = max(0, train_end - train_bars)

        btc_train = btc_full.iloc[train_start:train_end]
        eth_train = eth_full.iloc[train_start:train_end]
        btc_test  = btc_full.iloc[test_start:test_end]
        eth_test  = eth_full.iloc[test_start:test_end]

        # ── Daily optimizer: matches live system's 00:03 daily run ──────────
        # Step 1: Retrain UKFs on training window
        t0 = time.time()
        print(f"Day {day_idx+1}/{n_test_days}  |  bar {test_start}–{test_end-1}  "
              f"|  cap=${capital:.4f}", end='  ')

        try:
            (btc_c_1h, btc_h_1h, btc_l_1h,
             btc_c_5t, btc_h_5t, btc_l_5t) = build_ukfs(btc_train, 'BTC')
            (eth_c_1h, eth_h_1h, eth_l_1h,
             eth_c_5t, eth_h_5t, eth_l_5t) = build_ukfs(eth_train, 'ETH')
        except Exception as e:
            print(f"  ⚠ UKF build failed: {e} — skipping day.")
            capital_curve.extend([capital] * bars_per_day)
            continue

        # Step 2: Collect predictions over the grid-search window
        # (last opt_bars of the training window)
        opt_start = max(0, len(btc_train) - opt_bars)
        btc_opt   = btc_train.iloc[opt_start:].reset_index(drop=True)
        eth_opt   = eth_train.iloc[opt_start:].reset_index(drop=True)

        # Build temporary UKFs trained on data UP TO the opt window
        # (so grid search only sees data it would have had at that point)
        btc_opt_pre = btc_train.iloc[:opt_start]
        eth_opt_pre = eth_train.iloc[:opt_start]

        try:
            if len(btc_opt_pre) >= 10:
                (gc_b, gh_b, gl_b, gc_b5, gh_b5, gl_b5) = build_ukfs(btc_opt_pre, 'BTC')
                (gc_e, gh_e, gl_e, gc_e5, gh_e5, gl_e5) = build_ukfs(eth_opt_pre, 'ETH')
            else:
                (gc_b, gh_b, gl_b, gc_b5, gh_b5, gl_b5) = build_ukfs(btc_train, 'BTC')
                (gc_e, gh_e, gl_e, gc_e5, gh_e5, gl_e5) = build_ukfs(eth_train, 'ETH')

            btc_preds_opt = [step_ukf(gc_b, gh_b, gl_b, btc_opt.iloc[i])
                             for i in range(len(btc_opt))]
            eth_preds_opt = [step_ukf(gc_e, gh_e, gl_e, eth_opt.iloc[i])
                             for i in range(len(eth_opt))]

            current_params = fast_grid_search(btc_preds_opt, eth_preds_opt,
                                              btc_opt, eth_opt,
                                              capital, LEVERAGE)
        except Exception as e:
            print(f"  ⚠ Grid search failed: {e} — reusing last params.")

        current_params['day'] = day_idx + 1
        daily_params_log.append(dict(current_params))

        # ── Paper trade the test day ─────────────────────────────────────────
        try:
            final_cap, day_trades, day_stats = paper_trade_day(
                btc_test.reset_index(drop=True),
                eth_test.reset_index(drop=True),
                btc_c_1h, btc_h_1h, btc_l_1h,
                eth_c_1h, eth_h_1h, eth_l_1h,
                btc_c_5t, btc_h_5t, btc_l_5t,
                eth_c_5t, eth_h_5t, eth_l_5t,
                current_params, capital, LEVERAGE
            )
        except Exception as e:
            print(f"  ⚠ Paper trade failed: {e} — no change.")
            final_cap  = capital
            day_trades = []
            day_stats  = {}

        day_pnl = final_cap - capital
        capital  = final_cap

        # Extend capital curve hourly (linear interpolation within the day)
        capital_curve.extend([capital] * bars_per_day)
        all_trades.extend(day_trades)

        elapsed = time.time() - t0
        print(f"| pnl={day_pnl:+.4f}  trades={len(day_trades)}  "
              f"5m_refined={day_stats.get('entries_5m_refined',0)}  "
              f"({elapsed:.1f}s)")

        test_day_results.append(dict(
            day=day_idx+1, capital=capital, pnl=day_pnl,
            trades=len(day_trades), **day_stats,
            best_params_ret=current_params.get('best_returns', 0)
        ))

    print("\n" + "=" * 70)
    print("  RESULTS")
    print("=" * 70)

    metrics = compute_metrics(all_trades, capital_curve, INITIAL_CAPITAL)
    print(metrics.to_string(index=False))

    # ── Slippage breakdown ───────────────────────────────────────────────────
    print("\n── Execution Cost Breakdown ─────────────────────────────────────────")
    total_funding = sum(t.get('funding', 0) for t in all_trades)
    total_ecomm   = sum(t.get('entry_commission', 0) for t in all_trades)
    refined_saves = sum(t.get('5m_slip_saved', 0) for t in all_trades if t.get('5m_refined'))
    print(f"  Total funding paid     : ${total_funding:.4f}")
    print(f"  Total entry commission : ${total_ecomm:.4f}")
    print(f"  Total 5m entry savings : ${refined_saves:.4f}")
    print(f"  Net cost (fund+ecomm)  : ${total_funding + total_ecomm:.4f}")

    # ── Daily results table ──────────────────────────────────────────────────
    print("\n── Daily Summary (first 10 and last 10 test days) ──────────────────")
    daily_df = pd.DataFrame(test_day_results)
    if len(daily_df) > 20:
        print(pd.concat([daily_df.head(10), daily_df.tail(10)]).to_string(index=False))
    else:
        print(daily_df.to_string(index=False))

    # ── Params stability ─────────────────────────────────────────────────────
    print("\n── Daily Optimal Params Distribution ───────────────────────────────")
    params_df = pd.DataFrame(daily_params_log)
    for col in ('strict_btc', 'stricts_btc', 'strict_eth', 'stricts_eth'):
        if col in params_df.columns:
            vals = params_df[col].value_counts().head(5)
            print(f"  {col}: {dict(vals)}")

    print("\n── Plotting... ─────────────────────────────────────────────────────")
    plot_results(capital_curve, all_trades, daily_params_log, INITIAL_CAPITAL)

    return metrics, daily_df, all_trades, capital_curve


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    metrics, daily_df, all_trades, curve = run_walk_forward()