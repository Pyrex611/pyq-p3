"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         PyQuant Walk-Forward Realistic Backtest — Clean 1H Edition          ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Processes 15 days of test data with verbose grid search and trade logs.     ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, math, time, warnings
from itertools import product
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

from filterpy.kalman import UnscentedKalmanFilter, MerweScaledSigmaPoints
from filterpy.common import Q_discrete_white_noise

warnings.filterwarnings('ignore')

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
LEVERAGE              = 10        # Target leverage
INITIAL_CAPITAL       = 10.0      # Starting USD Account Balance
UKF_TRAIN_DAYS        = 30        # Historical days to train UKF
GRID_SEARCH_DAYS      = 15        # Window size for parameter optimization
WALK_FORWARD_DAYS     = 15        # Shortened evaluation period for testing
TEST_START_DAY        = 30        # First day to begin trading (0-indexed)

# Execution Toggles
USE_PESSIMISTIC_EXITS = True      # False = assume TP hits before SL on ambiguous candles
ENABLE_PLOTTING       = True      # False = bypass chart generation

# Data Paths
BTC_CSV = '/kaggle/input/pyquant-data/btc_back366.csv'
ETH_CSV = '/kaggle/input/pyquant-data/eth_back366.csv'

if not os.path.exists(BTC_CSV):
    BTC_CSV = 'btc_back366.csv'
    ETH_CSV = 'eth_back366.csv'

# ══════════════════════════════════════════════════════════════════════════════
# COST & EXCHANGE FEE CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
MAKER_FEE            = 0.0002   # 0.02% Fee
TAKER_FEE            = 0.0005   # 0.05% Fee
ENTRY_SLIPPAGE_PCT   = 0.0003   # 0.03% Slippage
TP_EXIT_SLIPPAGE_PCT = 0.0004   # 0.04% Slippage
SL_EXIT_SLIPPAGE_PCT = 0.0008   # 0.08% Slippage
FUNDING_RATE_PER_8H  = 0.0001   # 0.01% Per 8 hours
MIN_NOTIONAL         = 5.0      # Minimum USD Order Notional
BTC_LOT_STEP         = 0.001
ETH_LOT_STEP         = 0.001

# Grid search parameter space
STRICT_BTC_VALS  = [0.0025, 0.00275, 0.003, 0.0035, 0.004, 0.0045, 0.005]
STRICTS_BTC_VALS = [0.004,  0.0045,  0.005, 0.0055, 0.006, 0.007,  0.0075]
STRICT_ETH_VALS  = [0.0035, 0.004,   0.0045, 0.005, 0.0055, 0.006]
STRICTS_ETH_VALS = [0.0045, 0.005,   0.0055, 0.006, 0.0065, 0.007, 0.0075, 0.008]

# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNTING & TRANSACTION HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def floor_to_lot(qty: float, step: float) -> float:
    return math.floor(qty / step) * step

def apply_entry(signal_price: float, side: str, capital: float, leverage: float, lot_step: float):
    slip = ENTRY_SLIPPAGE_PCT
    filled = signal_price * (1 + slip) if side == 'Buy' else signal_price * (1 - slip)
    raw_qty  = (capital * leverage) / filled
    qty      = floor_to_lot(raw_qty, lot_step)
    notional = qty * filled
    valid    = qty > 0 and notional >= MIN_NOTIONAL
    commission = MAKER_FEE * notional
    return filled, qty, commission, valid

def apply_exit(trigger_price: float, leg: str, side: str, qty: float):
    slip = SL_EXIT_SLIPPAGE_PCT if leg == 'SL' else TP_EXIT_SLIPPAGE_PCT
    filled = trigger_price * (1 - slip) if side == 'Buy' else trigger_price * (1 + slip)
    commission = TAKER_FEE * qty * filled
    return filled, commission

def market_exit(price: float, qty: float):
    return price, (TAKER_FEE * qty * price)

def funding_cost(entry_ts, exit_ts, qty: float, entry_price: float) -> float:
    if entry_ts is None or exit_ts is None: return 0.0
    try:
        ed, xd = pd.to_datetime(entry_ts), pd.to_datetime(exit_ts)
        if pd.isna(ed) or pd.isna(xd): return 0.0
        periods = int((xd - ed).total_seconds() // (8 * 3600))
        return max(0, periods) * FUNDING_RATE_PER_8H * qty * entry_price
    except:
        return 0.0

def compute_profit(side: str, entry_price: float, exit_price: float, qty: float) -> float:
    return (exit_price - entry_price) * qty if side == 'Buy' else (entry_price - exit_price) * qty

# ══════════════════════════════════════════════════════════════════════════════
# UKF FILTER ENGINE (1H ONLY)
# ══════════════════════════════════════════════════════════════════════════════
def _stabilise_P(ukf, floor: float = 1e-6):
    P = (ukf.P + ukf.P.T) / 2
    for i in range(P.shape[0]):
        if P[i, i] < floor: P[i, i] = floor
    ukf.P = P

def _make_ukf(init_val: float, alpha: float, beta: float, kappa: float, P_init: float, Q_scale: float, R_init: float) -> UnscentedKalmanFilter:
    n_state, n_meas = 2, 1
    points = MerweScaledSigmaPoints(n=n_state, alpha=alpha, beta=beta, kappa=kappa)
    fx = lambda x, dt: np.array([x[0] + dt * x[1], x[1]])
    hx = lambda x: np.array([x[0]])
    ukf = UnscentedKalmanFilter(dim_x=n_state, dim_z=n_meas, fx=fx, hx=hx, dt=1, points=points)
    ukf.P, ukf.R = np.eye(n_state) * P_init, np.eye(n_meas) * R_init
    ukf.Q = Q_discrete_white_noise(dim=n_state, dt=1, var=0.004) * Q_scale
    ukf.x = np.array([init_val, 0.0])
    return ukf

def _train_ukf(values: np.ndarray, alpha, beta, kappa, P_init, Q_scale, R_init):
    ukf = _make_ukf(values[0], alpha, beta, kappa, P_init, Q_scale, R_init)
    for z in values:
        ukf.predict(); _stabilise_P(ukf)
        ukf.update(z); _stabilise_P(ukf)
    ukf.predict(); _stabilise_P(ukf)
    return ukf

def build_ukfs(df: pd.DataFrame, symbol: str):
    p = {'alpha': 0.001, 'beta': 7.0, 'kappa': 0, 'P': 0.001, 'Q': 1.0, 'R': 0.01} if symbol == 'BTC' else \
        {'alpha': 0.001, 'beta': 4.0, 'kappa': 1, 'P': 0.1,   'Q': 1.0, 'R': 0.01}
    c, h, l = df['close'].values, df['high'].values, df['low'].values
    return (_train_ukf(c, p['alpha'], p['beta'], p['kappa'], p['P'], p['Q'], p['R']),
            _train_ukf(h, p['alpha'], p['beta'], p['kappa'], p['P'], p['Q'], p['R']),
            _train_ukf(l, p['alpha'], p['beta'], p['kappa'], p['P'], p['Q'], p['R']))

def step_ukf(ukf, high_ukf, low_ukf, row: pd.Series):
    for u, val in [(ukf, row['close']), (high_ukf, row['high']), (low_ukf, row['low'])]:
        u.update(val);  _stabilise_P(u)
        u.predict();    _stabilise_P(u)
    return ukf.x[0], high_ukf.x[0], low_ukf.x[0]

# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL GENERATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════
def _symbol_params(symbol: str, strict: float, stricts: float) -> dict:
    if symbol == 'BTC':
        return dict(first_loss=7.5, loss_multiple=30, rounds=0, tp_inc=1.004, lot_step=BTC_LOT_STEP)
    return dict(first_loss=16, loss_multiple=1, rounds=2, tp_inc=1.05, lot_step=ETH_LOT_STEP)

def generate_signal(price: float, pred: float, high_pred: float, low_pred: float, symbol: str, balance: float, leverage: int, strict: float, stricts: float):
    sp = _symbol_params(symbol, strict, stricts)
    r  = sp['rounds']
    max_loss = balance * sp['first_loss'] * sp['loss_multiple']

    if pred > price:
        trend, pct_diff, vol = 'up', abs(1 - pred / price), abs(1 - high_pred / pred)
        if not (pct_diff <= stricts or vol >= stricts): return None
        buy_price = low_pred
        if not (pred > buy_price and (abs(1 - pred / buy_price) if buy_price > 0 else 0) >= strict): return None
        entry = round(price, r) if price < buy_price else round(buy_price, r)
        order_type = 'Market' if price < buy_price else 'Limit'
        tp_cand = ((balance * sp['tp_inc']) * leverage) / ((balance * leverage) / entry) if entry > 0 else pred
        tp = round(tp_cand if pred > tp_cand else pred, r)
        pos = balance / entry if entry > 0 else 0
        sl  = round(entry - max_loss / (entry * pos), r) if pos > 0 and entry > 0 else entry * 0.98
        return dict(symbol=symbol, order_type=order_type, order_side='Buy', entry_price=entry, tp_price=tp, sl_price=sl, current_price=price)
    elif price > pred:
        trend, pct_diff, vol = 'down', abs(1 - price / pred), (abs(1 - pred / low_pred) if low_pred > 0 else 0)
        if not (pct_diff <= stricts or vol >= stricts): return None
        sell_price = high_pred
        if not (sell_price > pred and (abs(1 - sell_price / pred) if pred > 0 else 0) >= strict): return None
        entry = round(price, r) if price > sell_price else round(sell_price, r)
        order_type = 'Market' if price > sell_price else 'Limit'
        tp_cand = ((balance * (2 - sp['tp_inc'])) * leverage) / ((balance * leverage) / entry) if entry > 0 else pred
        tp = round(tp_cand if pred < tp_cand else pred, r)
        pos = balance / entry if entry > 0 else 0
        sl  = round(entry + max_loss / (entry * pos), r) if pos > 0 and entry > 0 else entry * 1.02
        return dict(symbol=symbol, order_type=order_type, order_side='Sell', entry_price=entry, tp_price=tp, sl_price=sl, current_price=price)
    return None

# ══════════════════════════════════════════════════════════════════════════════
# FAST GRID SEARCH
# ══════════════════════════════════════════════════════════════════════════════
def fast_grid_search(btc_preds: list, eth_preds: list, btc_bars: pd.DataFrame, eth_bars: pd.DataFrame, balance: float, leverage: int) -> dict:
    best_ret = -np.inf
    best_params = None
    n_bars = min(len(btc_preds), len(btc_bars), len(eth_preds), len(eth_bars))

    for sb, sbs, se, ses in product(STRICT_BTC_VALS, STRICTS_BTC_VALS, STRICT_ETH_VALS, STRICTS_ETH_VALS):
        equity = balance
        in_btc = in_eth = False
        entry_btc = entry_eth = 0.0
        qty_btc = qty_eth = 0.0
        tp_btc = sl_btc = tp_eth = sl_eth = 0.0
        side_btc = side_eth = ''

        for i in range(n_bars):
            row_b, row_e = btc_bars.iloc[i], eth_bars.iloc[i]
            pb, hb, lb = row_b['close'], row_b['high'], row_b['low']
            pe, he, le = row_e['close'], row_e['high'], row_e['low']

            # --- BTC Position Tracking ---
            if in_btc:
                h_sl, h_tp = (lb <= sl_btc if side_btc == 'Buy' else hb >= sl_btc), (hb >= tp_btc if side_btc == 'Buy' else lb <= tp_btc)
                if h_sl and h_tp: 
                    if USE_PESSIMISTIC_EXITS: h_tp = False
                    else: h_sl = False
                if h_sl:
                    xp, xc = apply_exit(sl_btc, 'SL', side_btc, qty_btc)
                    equity += compute_profit(side_btc, entry_btc, xp, qty_btc) - xc
                    in_btc = False
                elif h_tp:
                    xp, xc = apply_exit(tp_btc, 'TP', side_btc, qty_btc)
                    equity += compute_profit(side_btc, entry_btc, xp, qty_btc) - xc
                    in_btc = False

            # --- ETH Position Tracking ---
            if in_eth:
                h_sl, h_tp = (le <= sl_eth if side_eth == 'Buy' else he >= sl_eth), (he >= tp_eth if side_eth == 'Buy' else le <= tp_eth)
                if h_sl and h_tp: 
                    if USE_PESSIMISTIC_EXITS: h_tp = False
                    else: h_sl = False
                if h_sl:
                    xp, xc = apply_exit(sl_eth, 'SL', side_eth, qty_eth)
                    equity += compute_profit(side_eth, entry_eth, xp, qty_eth) - xc
                    in_eth = False
                elif h_tp:
                    xp, xc = apply_exit(tp_eth, 'TP', side_eth, qty_eth)
                    equity += compute_profit(side_eth, entry_eth, xp, qty_eth) - xc
                    in_eth = False

            if in_btc or in_eth: continue

            # --- Evaluation ---
            pred_b, hp_b, lp_b = btc_preds[i]
            pred_e, hp_e, lp_e = eth_preds[i]
            sig_b = generate_signal(pb, pred_b, hp_b, lp_b, 'BTC', equity, leverage, sb, sbs)
            sig_e = generate_signal(pe, pred_e, hp_e, lp_e, 'ETH', equity, leverage, se, ses)

            alloc = equity / 2 if (sig_b and sig_e) else (equity * 0.8 if (sig_b or sig_e) else 0)
            if alloc == 0: continue

            if sig_b and not in_btc:
                ep, qty, ec, valid = apply_entry(sig_b['entry_price'], sig_b['order_side'], alloc, leverage, BTC_LOT_STEP)
                if valid:
                    in_btc, side_btc, entry_btc, qty_btc, tp_btc, sl_btc = True, sig_b['order_side'], ep, qty, sig_b['tp_price'], sig_b['sl_price']
                    equity -= ec

            if sig_e and not in_eth:
                ep, qty, ec, valid = apply_entry(sig_e['entry_price'], sig_e['order_side'], alloc, leverage, ETH_LOT_STEP)
                if valid:
                    in_eth, side_eth, entry_eth, qty_eth, tp_eth, sl_eth = True, sig_e['order_side'], ep, qty, sig_e['tp_price'], sig_e['sl_price']
                    equity -= ec

        ret = (equity / balance - 1) * 100
        if ret > best_ret and ret > 0:
            best_ret = ret
            best_params = {'strict_btc': sb, 'stricts_btc': sbs, 'strict_eth': se, 'stricts_eth': ses, 'best_returns': ret}

    return best_params

# ══════════════════════════════════════════════════════════════════════════════
# VERBOSE ONSITE PAPER-TRADING LOOP
# ══════════════════════════════════════════════════════════════════════════════
def paper_trade_day(btc_day: pd.DataFrame, eth_day: pd.DataFrame, btc_filters, eth_filters, params: dict, current_equity: float, leverage: int):
    equity = current_equity
    trades = []
    in_b = in_e = False
    pos_b, pos_e = {}, {}
    
    btc_ukf_c, btc_ukf_h, btc_ukf_l = btc_filters
    eth_ukf_c, eth_ukf_h, eth_ukf_l = eth_filters

    sb, sbs = params['strict_btc'], params['stricts_btc']
    se, ses = params['strict_eth'], params['stricts_eth']

    for idx in range(len(btc_day)):
        row_b, row_e = btc_day.iloc[idx], eth_day.iloc[idx]
        ts = str(row_b.get('timestamp', row_b.name))

        pred_b, hp_b, lp_b = step_ukf(btc_ukf_c, btc_ukf_h, btc_ukf_l, row_b)
        pred_e, hp_e, lp_e = step_ukf(eth_ukf_c, eth_ukf_h, eth_ukf_l, row_e)

        pb, hb, lb = row_b['close'], row_b['high'], row_b['low']
        pe, he, le = row_e['close'], row_e['high'], row_e['low']

        # ─── BTC Execution Verification ───
        if in_b:
            side, ep, qty, tp, sl = pos_b['side'], pos_b['entry_price'], pos_b['qty'], pos_b['tp_price'], pos_b['sl_price']
            h_sl, h_tp = (lb <= sl if side == 'Buy' else hb >= sl), (hb >= tp if side == 'Buy' else lb <= tp)
            
            exit_leg, exit_trig = None, None
            if h_sl and h_tp:
                exit_leg, exit_trig = ('SL', sl) if USE_PESSIMISTIC_EXITS else ('TP', tp)
            elif h_sl: exit_leg, exit_trig = 'SL', sl
            elif h_tp: exit_leg, exit_trig = 'TP', tp
            
            if not exit_leg: # Dynamic Reversal Threshold Check
                pnl_check = compute_profit(side, ep, hb if side == 'Buy' else lb, qty)
                if pnl_check >= (qty * ep / leverage) * 0.225:
                    exit_leg, exit_trig = 'MARKET', (hb if side == 'Buy' else lb)

            if exit_leg:
                xp, xc = apply_exit(exit_trig, exit_leg, side, qty) if exit_leg in ('SL','TP') else market_exit(exit_trig, qty)
                pnl = compute_profit(side, ep, xp, qty)
                fc = funding_cost(pos_b['ts'], ts, qty, ep)
                net_pnl = pnl - xc - fc
                equity += net_pnl
                
                trade_record = {**pos_b, 'exit_price': xp, 'exit_ts': ts, 'exit_leg': exit_leg, 'profit': net_pnl, 'funding': fc}
                trades.append(trade_record)
                in_b = False
                print(f"  [TRADE EXIT]  {ts} | BTC {side} Closed via {exit_leg} at ${xp:,.2f} | PnL: ${net_pnl:+.4f} | Equity: ${equity:.4f}")

        # ─── ETH Execution Verification ───
        if in_e:
            side, ep, qty, tp, sl = pos_e['side'], pos_e['entry_price'], pos_e['qty'], pos_e['tp_price'], pos_e['sl_price']
            h_sl, h_tp = (le <= sl if side == 'Buy' else he >= sl), (he >= tp if side == 'Buy' else le <= tp)
            
            exit_leg, exit_trig = None, None
            if h_sl and h_tp:
                exit_leg, exit_trig = ('SL', sl) if USE_PESSIMISTIC_EXITS else ('TP', tp)
            elif h_sl: exit_leg, exit_trig = 'SL', sl
            elif h_tp: exit_leg, exit_trig = 'TP', tp
            
            if not exit_leg:
                pnl_check = compute_profit(side, ep, he if side == 'Buy' else le, qty)
                if pnl_check >= (qty * ep / leverage) * 0.175:
                    exit_leg, exit_trig = 'MARKET', (he if side == 'Buy' else le)

            if exit_leg:
                xp, xc = apply_exit(exit_trig, exit_leg, side, qty) if exit_leg in ('SL','TP') else market_exit(exit_trig, qty)
                pnl = compute_profit(side, ep, xp, qty)
                fc = funding_cost(pos_e['ts'], ts, qty, ep)
                net_pnl = pnl - xc - fc
                equity += net_pnl
                
                trade_record = {**pos_e, 'exit_price': xp, 'exit_ts': ts, 'exit_leg': exit_leg, 'profit': net_pnl, 'funding': fc}
                trades.append(trade_record)
                in_e = False
                print(f"  [TRADE EXIT]  {ts} | ETH {side} Closed via {exit_leg} at ${xp:,.2f} | PnL: ${net_pnl:+.4f} | Equity: ${equity:.4f}")

        if in_b or in_e: continue

        # ─── Entry Signals Evaluation ───
        sig_b = generate_signal(pb, pred_b, hp_b, lp_b, 'BTC', equity, leverage, sb, sbs)
        sig_e = generate_signal(pe, pred_e, hp_e, lp_e, 'ETH', equity, leverage, se, ses)

        alloc_b = alloc_e = equity
        if sig_b and sig_e: alloc_b = alloc_e = equity / 2
        elif sig_b: alloc_b = equity * 0.8
        elif sig_e: alloc_e = equity * 0.8

        if sig_b and not in_b:
            ep, qty, ec, valid = apply_entry(sig_b['entry_price'], sig_b['order_side'], alloc_b, leverage, BTC_LOT_STEP)
            if valid:
                equity -= ec
                pos_b = {'symbol':'BTC', 'side':sig_b['order_side'], 'entry_price':ep, 'qty':qty, 'tp_price':sig_b['tp_price'], 'sl_price':sig_b['sl_price'], 'ts':ts}
                in_b = True
                print(f"  [TRADE ENTRY] {ts} | BTC {sig_b['order_side']} Executed at ${ep:,.2f} | TP: ${sig_b['tp_price']:,.2f} | SL: ${sig_b['sl_price']:,.2f} | Qty: {qty}")

        if sig_e and not in_e:
            ep, qty, ec, valid = apply_entry(sig_e['entry_price'], sig_e['order_side'], alloc_e, leverage, ETH_LOT_STEP)
            if valid:
                equity -= ec
                pos_e = {'symbol':'ETH', 'side':sig_e['order_side'], 'entry_price':ep, 'qty':qty, 'tp_price':sig_e['tp_price'], 'sl_price':sig_e['sl_price'], 'ts':ts}
                in_e = True
                print(f"  [TRADE ENTRY] {ts} | ETH {sig_e['order_side']} Executed at ${ep:,.2f} | TP: ${sig_e['tp_price']:,.2f} | SL: ${sig_e['sl_price']:,.2f} | Qty: {qty}")

    # Force Liquidate EOD Open Positions
    if in_b:
        lc, lts = btc_day.iloc[-1]['close'], str(btc_day.iloc[-1].name)
        xp, xc = market_exit(lc, pos_b['qty'])
        net_pnl = compute_profit(pos_b['side'], pos_b['entry_price'], xp, pos_b['qty']) - xc
        equity += net_pnl
        trades.append({**pos_b, 'exit_price': xp, 'exit_ts': lts, 'exit_leg': 'EOD', 'profit': net_pnl, 'funding': 0.0})
        print(f"  [EOD FORCE CLOSE] BTC Position Liquidated at ${xp:,.2f} | PnL: ${net_pnl:+.4f}")
    if in_e:
        lc, lts = eth_day.iloc[-1]['close'], str(eth_day.iloc[-1].name)
        xp, xc = market_exit(lc, pos_e['qty'])
        net_pnl = compute_profit(pos_e['side'], pos_e['entry_price'], xp, pos_e['qty']) - xc
        equity += net_pnl
        trades.append({**pos_e, 'exit_price': xp, 'exit_ts': lts, 'exit_leg': 'EOD', 'profit': net_pnl, 'funding': 0.0})
        print(f"  [EOD FORCE CLOSE] ETH Position Liquidated at ${xp:,.2f} | PnL: ${net_pnl:+.4f}")

    return equity, trades

# ══════════════════════════════════════════════════════════════════════════════
# DATA PROCESSING & RESULTS CORE GRAPHING
# ══════════════════════════════════════════════════════════════════════════════
def run_walk_forward():
    print("=" * 80)
    print("  PyQuant Clean 1H Walk-Forward Realistic Backtest Engine")
    print(f"  Leverage: {LEVERAGE}x  |  Initial Capital: ${INITIAL_CAPITAL}  |  Duration: {WALK_FORWARD_DAYS} Days")
    print(f"  Pessimistic Exits Mode: {USE_PESSIMISTIC_EXITS}  |  Plotting Engine: {ENABLE_PLOTTING}")
    print("=" * 80)

    btc_full = pd.read_csv(BTC_CSV)
    eth_full = pd.read_csv(ETH_CSV)
    btc_full.columns = [c.lower() for c in btc_full.columns]
    eth_full.columns = [c.lower() for c in eth_full.columns]

    for df in (btc_full, eth_full):
        t_col = 'timestamp' if 'timestamp' in df.columns else 'time'
        df['timestamp'] = pd.to_datetime(df[t_col])
        df.set_index('timestamp', inplace=True)

    common_idx = btc_full.index.intersection(eth_full.index)
    btc_full = btc_full.loc[common_idx].reset_index()
    eth_full = eth_full.loc[common_idx].reset_index()

    capital = INITIAL_CAPITAL
    capital_curve = [capital]
    all_trades = []
    daily_params_log = []

    bars_per_day = 24
    train_bars = UKF_TRAIN_DAYS * bars_per_day
    opt_bars   = GRID_SEARCH_DAYS * bars_per_day
    start_bar  = TEST_START_DAY * bars_per_day

    default_params = {'strict_btc': 0.0025, 'stricts_btc': 0.005, 'strict_eth': 0.0035, 'stricts_eth': 0.0075, 'best_returns': 0.0}

    for day_idx in range(WALK_FORWARD_DAYS):
        test_start = start_bar + day_idx * bars_per_day
        test_end   = test_start + bars_per_day
        if test_end > len(btc_full): break

        btc_train = btc_full.iloc[max(0, test_start - train_bars):test_start]
        eth_train = eth_full.iloc[max(0, test_start - train_bars):test_start]
        btc_test  = btc_full.iloc[test_start:test_end]
        eth_test  = eth_full.iloc[test_start:test_end]

        print(f"\n▶ Day {day_idx + 1}/{WALK_FORWARD_DAYS} | Window Bars: {test_start} to {test_end-1} | Balance: ${capital:.4f}")

        # Retrain Filters 
        btc_filters = build_ukfs(btc_train, 'BTC')
        eth_filters = build_ukfs(eth_train, 'ETH')

        # Optimization Pipeline
        opt_start = max(0, len(btc_train) - opt_bars)
        btc_opt, eth_opt = btc_train.iloc[opt_start:].reset_index(drop=True), eth_train.iloc[opt_start:].reset_index(drop=True)
        btc_opt_pre, eth_opt_pre = btc_train.iloc[:opt_start], eth_train.iloc[:opt_start]

        try:
            pre_filters_b = build_ukfs(btc_opt_pre, 'BTC') if len(btc_opt_pre) >= 10 else btc_filters
            pre_filters_e = build_ukfs(eth_opt_pre, 'ETH') if len(eth_opt_pre) >= 10 else eth_filters
            
            b_preds = [step_ukf(pre_filters_b[0], pre_filters_b[1], pre_filters_b[2], btc_opt.iloc[i]) for i in range(len(btc_opt))]
            e_preds = [step_ukf(pre_filters_e[0], pre_filters_e[1], pre_filters_e[2], eth_opt.iloc[i]) for i in range(len(eth_opt))]

            opt_res = fast_grid_search(b_preds, e_preds, btc_opt, eth_opt, capital, LEVERAGE)
            if opt_res is not None:
                current_params = opt_res
                print(f"  [GRID SEARCH] Optimal Framework Decoupled: BTC({current_params['strict_btc']}/{current_params['stricts_btc']}) | ETH({current_params['strict_eth']}/{current_params['stricts_eth']}) | Return Vector: {current_params['best_returns']:.2f}%")
            else:
                current_params = default_params.copy()
                print("  ⚠️ [GRID SEARCH ALERT] No profitable hyperparameter combination found — deploying factory defaults.")
        except Exception as e:
            current_params = default_params.copy()
            print(f"  ⚠️ [GRID SEARCH ERROR] Processing Exception encountered ({e}) — deploying factory defaults.")

        current_params['day'] = day_idx + 1
        daily_params_log.append(dict(current_params))

        # Paper Trade Processing Execution
        final_cap, day_trades = paper_trade_day(
            btc_test.reset_index(drop=True), eth_test.reset_index(drop=True),
            btc_filters, eth_filters, current_params, capital, LEVERAGE
        )

        day_pnl = final_cap - capital
        capital = final_cap
        capital_curve.extend([capital] * bars_per_day)
        all_trades.extend(day_trades)

    print("\n" + "=" * 80)
    print("  FINAL PERFORMANCE ABSTRACT")
    print("=" * 80)
    print(f"  Ending Account Balance : ${capital:.4f}")
    print(f"  Total Trades Processed : {len(all_trades)}")
    print(f"  Net Performance Change : {((capital / INITIAL_CAPITAL) - 1) * 100:+.2f}%")
    print("=" * 80)

if __name__ == '__main__':
    run_walk_forward()