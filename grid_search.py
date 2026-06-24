"""
grid_search.py
Grid search over BTC/ETH signal-threshold parameters using the backtester.
Exposes run_grid_search() for the daily_optimizer in pyquant_orchestra.py.
"""

import os
import time
import math
import numpy as np
import pandas as pd
from itertools import product

from sklearn.metrics import (
    mean_squared_error, mean_absolute_percentage_error,
    mean_absolute_error, r2_score
)

from filterpy.kalman import UnscentedKalmanFilter, MerweScaledSigmaPoints
from filterpy.common import Q_discrete_white_noise


# ---------------------------------------------------------------------------
# Helpers shared with backtest
# ---------------------------------------------------------------------------

def aggregate_ohlcv_data(data: pd.DataFrame, aggregation_minutes: int) -> pd.DataFrame:
    """Re-samples 1-minute OHLCV data to `aggregation_minutes` bars."""
    agg_rules = {k: v for k, v in {
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
        "trade_count": "sum", "vwap": "mean"
    }.items() if k in data.columns}

    if not isinstance(data.index, pd.DatetimeIndex):
        data.index = pd.to_datetime(data.index)

    return data.resample(f"{aggregation_minutes}T").agg(agg_rules)


def ukf_factory(timeframe: str, or_symbols: list):
    """Creates and trains three UKFs (close, high, low) for the given symbol."""
    UKF_DAYS = 30
    ukf_rows = UKF_DAYS * 24

    if or_symbols == ["BTC/USD"]:
        symb = "BTC"
        df_raw = pd.read_csv("btc_back365.csv")
    elif or_symbols == ["ETH/USD"]:
        symb = "ETH"
        df_raw = pd.read_csv("eth_back365.csv")
    else:
        raise ValueError(f"Unknown symbol list: {or_symbols}")

    crypto_bars_df = df_raw[:ukf_rows]

    if timeframe == "1H":
        data = crypto_bars_df
    elif timeframe == "15T":
        data = aggregate_ohlcv_data(crypto_bars_df.copy(), 15)
        data.dropna(inplace=True)
    elif timeframe == "5T":
        data = aggregate_ohlcv_data(crypto_bars_df.copy(), 5)
        data.dropna(inplace=True)
    else:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    dt_step = 1
    n_dim_state, n_dim_meas = 2, 1

    def fx(x, dt): return np.array([x[0] + dt * x[1], x[1]])
    def hx(x):     return np.array([x[0]])

    close_prices = data["close"].values
    high_prices  = data["high"].values
    low_prices   = data["low"].values

    train_size = int(len(close_prices) * 0.7)
    train_data     = close_prices[:train_size]
    high_train     = high_prices[:train_size]
    low_train      = low_prices[:train_size]

    best_params = (
        {"alpha": 0.001, "beta": 4.0, "kappa": 1, "P": 0.1,   "Q": 1.0, "R": 0.01}
        if symb == "ETH" else
        {"alpha": 0.001, "beta": 7.0, "kappa": 0, "P": 0.001, "Q": 1.0, "R": 0.01}
    )

    alpha, beta, kappa = best_params["alpha"], best_params["beta"], best_params["kappa"]
    P, Q, R = best_params["P"], best_params["Q"], best_params["R"]
    points = MerweScaledSigmaPoints(n=n_dim_state, alpha=alpha, beta=beta, kappa=kappa)

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

    ukf      = _make_ukf(train_data[0])
    high_ukf = _make_ukf(high_train[0])
    low_ukf  = _make_ukf(low_train[0])

    for z in train_data:
        ukf.predict(); ukf.update(z)
    for z in close_prices[train_size:]:
        ukf.predict(); ukf.update(z)

    for z in high_train:
        high_ukf.predict(); high_ukf.update(z)
    for z in high_prices[train_size:]:
        high_ukf.predict(); high_ukf.update(z)

    for z in low_train:
        low_ukf.predict(); low_ukf.update(z)
    for z in low_prices[train_size:]:
        low_ukf.predict(); low_ukf.update(z)

    ukf.predict(); high_ukf.predict(); low_ukf.predict()

    return ukf, high_ukf, low_ukf


# ---------------------------------------------------------------------------
# Metric helpers (identical logic to pyQuant_backtest2 to avoid import cycle)
# ---------------------------------------------------------------------------

def calculate_sharpe_ratio(returns, risk_free_rate=0.0025):
    if len(returns) < 2:
        return 0
    arr = np.array(returns)
    excess = arr - risk_free_rate
    return np.mean(excess) / np.std(excess) * math.sqrt(365) if np.std(excess) != 0 else 0


def calculate_max_drawdown(trades, initial_capital, date_index):
    peak = initial_capital
    max_dd = 0
    current = initial_capital
    sorted_trades = sorted(trades, key=lambda x: x["entry_date"])
    ti = 0
    for date in date_index:
        while (ti < len(sorted_trades) and
               sorted_trades[ti]["exit_date"] is not None and
               sorted_trades[ti]["exit_date"] <= date):
            current += sorted_trades[ti]["profit"]
            ti += 1
        if current > peak:
            peak = current
        dd = (peak - current) / peak if peak != 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd


def _combined_return(trades, initial_capital, date_index, final_value):
    returns = final_value / initial_capital - 1 if initial_capital != 0 else 0
    return returns * 100


# ---------------------------------------------------------------------------
# Slim backtest used only for grid search (no print spam)
# ---------------------------------------------------------------------------

def _backtest(btc_val, eth_val,
              high_ukf_btc, low_ukf_btc, ukf_btc,
              high_ukf_eth, low_ukf_eth, ukf_eth,
              btc_strict, btc_stricts, eth_strict, eth_stricts,
              initial_capital=10):
    """
    Runs a backtest for one parameter combination.
    Returns total portfolio return (%).
    """
    trades_btc, trades_eth = [], []
    total_cap = initial_capital
    commission = 0.0025
    leverage = 20

    pos_btc = pos_eth = 0.0
    entry_btc = entry_eth = 0.0
    in_btc = in_eth = False
    tp_btc = sl_btc = tp_eth = sl_eth = 0.0
    lev_btc = lev_eth = leverage
    cap_btc = cap_eth = 0.0

    mkt_btc, mkt_eth = [], []

    for index, row_btc in btc_val.iterrows():
        row_eth = eth_val.loc[index]

        mkt_btc.append(row_btc["close"])
        mkt_eth.append(row_eth["close"])

        price_btc, high_btc, low_btc = row_btc["close"], row_btc["high"], row_btc["low"]
        price_eth, high_eth, low_eth = row_eth["close"], row_eth["high"], row_eth["low"]

        # --- BTC UKF ---
        high_ukf_btc.predict(); hpb = high_ukf_btc.x[0]; high_ukf_btc.update(high_btc)
        low_ukf_btc.predict();  lpb = low_ukf_btc.x[0];  low_ukf_btc.update(low_btc)
        ukf_btc.predict();      pb  = ukf_btc.x[0];       ukf_btc.update(price_btc)

        sig_btc = None
        cand_entry_btc = cand_tp_btc = None
        btc_buy_tp = 1.0075
        strict_btc  = btc_strict
        stricts_btc = btc_stricts

        if pb > price_btc:
            pct_b = abs(1 - pb / price_btc); vol_b = abs(1 - hpb / pb); trend_b = "up"
        elif price_btc > pb:
            pct_b = abs(1 - price_btc / pb); vol_b = abs(1 - pb / lpb);  trend_b = "down"
        else:
            pct_b = vol_b = 0; trend_b = "flat"

        if pct_b <= stricts_btc or vol_b >= stricts_btc:
            if trend_b == "up":
                prof = abs(1 - pb / lpb)
                if pb > lpb and prof >= strict_btc and low_btc <= lpb <= high_btc:
                    sig_btc = "BUY"
                    cand_entry_btc = lpb
                    cand_tp_btc = min(pb, lpb * btc_buy_tp)
            elif trend_b == "down":
                prof = abs(1 - hpb / pb)
                if hpb > pb and prof >= strict_btc and low_btc <= hpb <= high_btc:
                    sig_btc = "SELL"
                    cand_entry_btc = hpb
                    cand_tp_btc = min(pb, hpb * btc_buy_tp)

        # --- ETH UKF ---
        high_ukf_eth.predict(); hpe = high_ukf_eth.x[0]; high_ukf_eth.update(high_eth)
        low_ukf_eth.predict();  lpe = low_ukf_eth.x[0];  low_ukf_eth.update(low_eth)
        ukf_eth.predict();      pe  = ukf_eth.x[0];       ukf_eth.update(price_eth)

        sig_eth = None
        cand_entry_eth = cand_tp_eth = None
        eth_buy_tp = 1.0085
        eth_sell_tp = 2 - eth_buy_tp
        strict_eth_v  = eth_strict
        stricts_eth_v = eth_stricts

        if pe > price_eth:
            pct_e = abs(1 - pe / price_eth); vol_e = abs(1 - hpe / pe); trend_e = "up"
        elif price_eth > pe:
            pct_e = abs(1 - price_eth / pe); vol_e = abs(1 - pe / lpe);  trend_e = "down"
        else:
            pct_e = vol_e = 0; trend_e = "flat"

        if pct_e <= stricts_eth_v or vol_e >= stricts_eth_v:
            if trend_e == "up":
                prof = abs(1 - pe / lpe)
                if pe > lpe and prof >= strict_eth_v and low_eth <= lpe <= high_eth:
                    sig_eth = "BUY"
                    cand_entry_eth = lpe
                    cand_tp_eth = min(pe, lpe * eth_buy_tp)
            elif trend_e == "down":
                prof = abs(1 - hpe / pe)
                if hpe > pe and prof >= strict_eth_v and low_eth <= hpe <= high_eth:
                    sig_eth = "SELL"
                    cand_entry_eth = hpe
                    cand_tp_eth = min(pe, hpe * eth_sell_tp)

        # --- Portfolio allocation ---
        Max_Loss     = total_cap * 15
        btc_Max_Loss = total_cap * 8.5 * 32.5

        if sig_btc and sig_eth and not in_btc and not in_eth:
            cap_btc = cap_eth = total_cap / 2
            total_cap = 0
            entry_btc = cand_entry_btc; tp_btc = cand_tp_btc
            p_bt = cap_btc / entry_btc; pos_btc = p_bt - commission * p_bt
            ml = btc_Max_Loss / (entry_btc * pos_btc)
            sl_btc = entry_btc - ml if sig_btc == "BUY" else entry_btc + ml
            in_btc = True
            trades_btc.append({"asset": "BTC", "entry_date": row_btc.name, "entry_price": entry_btc, "signal": sig_btc, "exit_date": None, "exit_price": None, "profit": 0, "tp_price": tp_btc, "sl_price": sl_btc})

            entry_eth = cand_entry_eth; tp_eth = cand_tp_eth
            p_et = cap_eth / entry_eth; pos_eth = p_et - commission * p_et
            ml = Max_Loss / (entry_eth * pos_eth)
            sl_eth = entry_eth - ml if sig_eth == "BUY" else entry_eth + ml
            in_eth = True
            trades_eth.append({"asset": "ETH", "entry_date": row_eth.name, "entry_price": entry_eth, "signal": sig_eth, "exit_date": None, "exit_price": None, "profit": 0, "tp_price": tp_eth, "sl_price": sl_eth})

        elif sig_btc and not sig_eth and not in_btc and not in_eth:
            cap_btc = total_cap; total_cap = 0
            entry_btc = cand_entry_btc; tp_btc = cand_tp_btc
            p_bt = cap_btc / entry_btc; pos_btc = p_bt - commission * p_bt
            ml = btc_Max_Loss / (entry_btc * pos_btc)
            sl_btc = entry_btc - ml if sig_btc == "BUY" else entry_btc + ml
            in_btc = True
            trades_btc.append({"asset": "BTC", "entry_date": row_btc.name, "entry_price": entry_btc, "signal": sig_btc, "exit_date": None, "exit_price": None, "profit": 0, "tp_price": tp_btc, "sl_price": sl_btc})

        elif not sig_btc and sig_eth and not in_btc and not in_eth:
            cap_eth = total_cap; total_cap = 0
            entry_eth = cand_entry_eth; tp_eth = cand_tp_eth
            p_et = cap_eth / entry_eth; pos_eth = p_et - commission * p_et
            ml = Max_Loss / (entry_eth * pos_eth)
            sl_eth = entry_eth - ml if sig_eth == "BUY" else entry_eth + ml
            in_eth = True
            trades_eth.append({"asset": "ETH", "entry_date": row_eth.name, "entry_price": entry_eth, "signal": sig_eth, "exit_date": None, "exit_price": None, "profit": 0, "tp_price": tp_eth, "sl_price": sl_eth})

        # --- BTC exit ---
        if in_btc:
            lt = trades_btc[-1]
            exit_b = False; exit_p_b = None
            if lt["signal"] == "BUY":
                if low_btc <= lt["sl_price"]:
                    exit_b = True; exit_p_b = lt["sl_price"]
                elif high_btc >= lt["tp_price"]:
                    exit_b = True; exit_p_b = lt["tp_price"]
                elif trend_b == "down" and pct_b >= 0.00005:
                    exit_b = True; exit_p_b = price_btc
            elif lt["signal"] == "SELL":
                if high_btc >= lt["sl_price"]:
                    exit_b = True; exit_p_b = lt["sl_price"]
                elif low_btc <= lt["tp_price"]:
                    exit_b = True; exit_p_b = lt["tp_price"]
                elif trend_b == "down" and pct_b >= 0.00005:
                    exit_b = True; exit_p_b = price_btc

            if exit_b:
                profit = lev_btc * ((exit_p_b - lt["entry_price"]) * pos_btc if lt["signal"] == "BUY" else (lt["entry_price"] - exit_p_b) * pos_btc)
                total_cap += (pos_btc * lt["entry_price"]) + profit
                pos_btc = 0; in_btc = False
                lt["exit_date"] = row_btc.name; lt["exit_price"] = exit_p_b; lt["profit"] = profit

        # --- ETH exit ---
        if in_eth:
            lt = trades_eth[-1]
            exit_e = False; exit_p_e = None
            if lt["signal"] == "BUY":
                if low_eth <= lt["sl_price"]:
                    exit_e = True; exit_p_e = lt["sl_price"]
                elif high_eth >= lt["tp_price"]:
                    exit_e = True; exit_p_e = lt["tp_price"]
                elif trend_e == "down" and pct_e >= 0.00005:
                    exit_e = True; exit_p_e = price_eth
            elif lt["signal"] == "SELL":
                if high_eth >= lt["sl_price"]:
                    exit_e = True; exit_p_e = lt["sl_price"]
                elif low_eth <= lt["tp_price"]:
                    exit_e = True; exit_p_e = lt["tp_price"]
                elif trend_e == "down" and pct_e >= 0.00005:
                    exit_e = True; exit_p_e = price_eth

            if exit_e:
                profit = lev_eth * ((exit_p_e - lt["entry_price"]) * pos_eth if lt["signal"] == "BUY" else (lt["entry_price"] - exit_p_e) * pos_eth)
                total_cap += (pos_eth * lt["entry_price"]) + profit
                pos_eth = 0; in_eth = False
                lt["exit_date"] = row_eth.name; lt["exit_price"] = exit_p_e; lt["profit"] = profit

    final = total_cap
    if in_btc and mkt_btc: final += pos_btc * mkt_btc[-1]
    if in_eth and mkt_eth: final += pos_eth * mkt_eth[-1]

    return (final / initial_capital - 1) * 100


# ---------------------------------------------------------------------------
# Public entry point called by pyquant_orchestra.py
# ---------------------------------------------------------------------------

def run_grid_search(backtest_days: int = 15, initial_capital: float = 10.0) -> dict:
    """
    Performs a grid search over signal thresholds and returns the best
    parameter set as a dict that can be JSON-serialised and saved.

    Returns:
        dict: {
            "strict_btc": float, "stricts_btc": float,
            "strict_eth": float, "stricts_eth": float,
            "best_returns": float
        }
    """
    # Load CSVs written by data_download()
    length = backtest_days * 24
    btc_valid = pd.read_csv("btc_back366.csv")
    eth_valid = pd.read_csv("eth_back366.csv")
    btc_val = btc_valid[-length:].reset_index(drop=True)
    eth_val = eth_valid[-length:].reset_index(drop=True)

    # Align indices so the backtest can use .loc[index]
    btc_val.index = range(len(btc_val))
    eth_val.index = range(len(eth_val))

    # Train UKFs once (shared across all param combos)
    btc_ukf, btc_high_ukf, btc_low_ukf = ukf_factory("1H", ["BTC/USD"])
    eth_ukf, eth_high_ukf, eth_low_ukf = ukf_factory("1H", ["ETH/USD"])

    # Parameter grid
    strict_btc_values  = [0.0025, 0.00275, 0.003, 0.0035, 0.004, 0.0045, 0.005]
    stricts_btc_values = [0.004,  0.0045,  0.005, 0.0055, 0.006, 0.007,  0.0075]
    strict_eth_values  = [0.0045, 0.0035,  0.004, 0.0055, 0.00475, 0.005, 0.006]
    stricts_eth_values = [0.0045, 0.005,   0.0055, 0.006, 0.0065, 0.007, 0.0075, 0.008]

    best_returns = -np.inf
    best_params  = None
    start_time   = time.time()

    for sb, sbs, se, ses in product(
        strict_btc_values, stricts_btc_values,
        strict_eth_values, stricts_eth_values
    ):
        # Re-train fresh UKFs for each combo so state doesn't bleed between runs
        b_ukf, b_high, b_low = ukf_factory("1H", ["BTC/USD"])
        e_ukf, e_high, e_low = ukf_factory("1H", ["ETH/USD"])

        try:
            ret = _backtest(
                btc_val, eth_val,
                b_high, b_low, b_ukf,
                e_high, e_low, e_ukf,
                sb, sbs, se, ses,
                initial_capital=initial_capital
            )
        except Exception as exc:
            print(f"[grid_search] Skipping combo ({sb},{sbs},{se},{ses}): {exc}")
            continue

        if ret > best_returns and ret > 0:
            best_returns = ret
            best_params  = (sb, sbs, se, ses)
            print(f"[grid_search] New best → returns={ret:.2f}%  params=({sb},{sbs},{se},{ses})")

    elapsed = time.time() - start_time
    h, rem  = divmod(elapsed, 3600)
    m, s    = divmod(rem, 60)
    print(f"[grid_search] Completed in {int(h)}h {int(m)}m {int(s)}s")

    if best_params is None:
        print("[grid_search] WARNING: No profitable combo found. Returning defaults.")
        return {
            "strict_btc": 0.0025, "stricts_btc": 0.005,
            "strict_eth": 0.0035, "stricts_eth": 0.0075,
            "best_returns": 0.0
        }

    return {
        "strict_btc":  best_params[0],
        "stricts_btc": best_params[1],
        "strict_eth":  best_params[2],
        "stricts_eth": best_params[3],
        "best_returns": best_returns
    }


if __name__ == "__main__":
    result = run_grid_search()
    print(result)
