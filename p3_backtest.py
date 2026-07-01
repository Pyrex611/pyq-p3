import numpy as np
import pandas as pd
import schedule
import alpaca_trade_api as tradeapi
import logging
import threading
import time
import math
import websocket
import json
from datetime import datetime, timedelta, timezone
from sklearn.metrics import mean_squared_error, mean_absolute_percentage_error, mean_absolute_error, r2_score
import matplotlib.pyplot as plt

from alpaca_trade_api.stream import Stream
from alpaca_trade_api.common import URL
import datetime as dt
from alpaca.trading.client import TradingClient
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.requests import StockBarsRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# For Real-Time Data
from alpaca.data.live.crypto import CryptoDataStream

import nest_asyncio
nest_asyncio.apply()

import requests
import hashlib
import hmac
import uuid

from filterpy.kalman import UnscentedKalmanFilter
from filterpy.kalman import MerweScaledSigmaPoints
from filterpy.common import Q_discrete_white_noise

# ══════════════════════════════════════════════════════════════════════════
# REALISM UPGRADE — Binance USDⓈ-M Futures execution costs and constraints
# ══════════════════════════════════════════════════════════════════════════

MAKER_FEE = 0.0002    # 0.02% — limit entry
TAKER_FEE = 0.0005    # 0.05% — stop-market / take-profit-market exit
FUNDING_RATE_PER_8H = 0.0001   # 0.01% per 8-hour funding interval

ENTRY_SLIPPAGE_PCT    = 0.0003   # 0.03% — LIMIT entry queue slippage
TP_EXIT_SLIPPAGE_PCT  = 0.0004   # 0.04% — TAKE_PROFIT_MARKET fill slippage
SL_EXIT_SLIPPAGE_PCT  = 0.0008   # 0.08% — STOP_MARKET fill slippage 

MIN_NOTIONAL = 5.0      # USDT, Binance Futures floor for BTCUSDT/ETHUSDT
BTC_LOT_STEP = 0.001
ETH_LOT_STEP = 0.001


def apply_realistic_entry(entry_price_candidate: float, side: str, capital: float,
                           leverage_val: float, lot_step: float):
    
    if side == "BUY":
        filled_price = entry_price_candidate * (1 + ENTRY_SLIPPAGE_PCT)
    else:
        filled_price = entry_price_candidate * (1 - ENTRY_SLIPPAGE_PCT)

    # Leverage applies to purchasing power, not the exit PnL
    raw_qty       = (capital * leverage_val) / filled_price
    position_size = math.floor(raw_qty / lot_step) * lot_step
    
    notional      = position_size * filled_price 
    is_valid      = position_size > 0 and notional >= MIN_NOTIONAL
    commission_fee = MAKER_FEE * notional # Fee charged on notional USDT value

    return filled_price, position_size, commission_fee, is_valid


def apply_realistic_exit(trigger_price: float, leg: str, side: str,
                          position_size: float):
    
    slip_pct = SL_EXIT_SLIPPAGE_PCT if leg == "SL" else TP_EXIT_SLIPPAGE_PCT

    if side == "BUY":
        filled_price = trigger_price * (1 - slip_pct)
    else:
        filled_price = trigger_price * (1 + slip_pct)

    # Fee charged on notional exit USDT value
    commission_fee = TAKER_FEE * (position_size * filled_price)
    return filled_price, commission_fee


def compute_funding_cost(entry_ts, exit_ts, position_size: float,
                          entry_price: float) -> float:
    
    if entry_ts is None or exit_ts is None:
        return 0.0

    try:
        entry_dt = pd.to_datetime(entry_ts)
        exit_dt  = pd.to_datetime(exit_ts)
        if pd.isna(entry_dt) or pd.isna(exit_dt):
            return 0.0
    except Exception:
        return 0.0

    hold_seconds = (exit_dt - entry_dt).total_seconds()
    if hold_seconds <= 0:
        return 0.0

    funding_periods = int(hold_seconds // (8 * 3600))
    if funding_periods <= 0:
        return 0.0

    # Leverage is baked into position_size, do not multiply by leverage again
    notional = position_size * entry_price
    return funding_periods * FUNDING_RATE_PER_8H * notional


def round_to_lot(qty: float, lot_step: float) -> float:
    return math.floor(qty / lot_step) * lot_step

# ══════════════════════════════════════════════════════════════════════════
# END REALISM UPGRADE BLOCK
# ══════════════════════════════════════════════════════════════════════════

def ukf_factory(timeframe, or_symbols): 
    global symb
    dt = 1   
    n_dim_state = 2  
    n_dim_meas = 1  
    frame = timeframe

    ukf_days = 30
    ukf_rows = ukf_days * 24
    print(or_symbols)
    if or_symbols == ['BTC/USD']:
        symb = "BTC"
        crypto_bars_d =pd.read_csv('btc_back365.csv')
        crypto_bars_df = crypto_bars_d[:ukf_rows]
    elif or_symbols == ['ETH/USD']: 
        symb = "ETH"
        crypto_bars_d =pd.read_csv('eth_back365.csv')
        crypto_bars_df = crypto_bars_d[:ukf_rows]
    else:
        print("Error with getting symb for or_symbols", )

    if frame == "1H":
        data = crypto_bars_df
    elif frame == "15T":
        agg_crypto_bars = aggregate_ohlcv_data(crypto_bars_df.copy(), aggregation_minutes=15)
        data = agg_crypto_bars.copy()
        data.dropna(inplace=True)
    elif frame == "5T":
        agg_crypto_bars = aggregate_ohlcv_data(crypto_bars_df.copy(), aggregation_minutes=5)
        data = agg_crypto_bars.copy()
        data.dropna(inplace=True)

    def fx(x, dt):
        return np.array([x[0] + dt * x[1], x[1]])

    def hx(x):
        return np.array([x[0]])

    def high_fx(x, dt):
        return np.array([x[0] + dt * x[1], x[1]])

    def high_hx(x):
        return np.array([x[0]])

    def low_fx(x, dt):
        return np.array([x[0] + dt * x[1], x[1]])

    def low_hx(x):
        return np.array([x[0]])

    close_prices = data['close'].values
    high_prices = data['high'].values
    low_prices = data['low'].values
    var = (close_prices.std()) ** 2

    train_size = int(len(close_prices) * 0.7)
    train_data, test_data = close_prices[:train_size], close_prices[train_size:]
    high_train_data, high_test_data = high_prices[:train_size], high_prices[train_size:]
    low_train_data, low_test_data = low_prices[:train_size], low_prices[train_size:]

    if symb == "ETH":
        best_params = {'alpha': 0.001, 'beta': 4.0, 'kappa': 1, 'P': 0.1, 'Q': 1.0, 'R': 0.01}
    elif symb == "BTC":
        best_params = {'alpha': 0.001, 'beta': 7.0, 'kappa': 0, 'P': 0.001, 'Q': 1.0, 'R': 0.01}

    alpha, beta, kappa = best_params['alpha'], best_params['beta'], best_params['kappa']
    P, Q, R = best_params['P'], best_params['Q'], best_params['R']
    points = MerweScaledSigmaPoints(n=n_dim_state, alpha=alpha, beta=beta, kappa=kappa)

    ukf = UnscentedKalmanFilter(dim_x=n_dim_state, dim_z=n_dim_meas, fx=fx, hx=hx, dt=dt, points=points)
    ukf.P = np.eye(n_dim_state) * P
    ukf.Q = Q_discrete_white_noise(dim=n_dim_state, dt=dt, var=0.004) * Q
    ukf.R = np.eye(n_dim_meas) * R
    ukf.x = np.array([train_data[0], 0])

    high_ukf = UnscentedKalmanFilter(dim_x=n_dim_state, dim_z=n_dim_meas, fx=high_fx, hx=high_hx, dt=dt, points=points)
    high_ukf.P = np.eye(n_dim_state) * P
    high_ukf.Q = Q_discrete_white_noise(dim=n_dim_state, dt=dt, var=0.004) * Q
    high_ukf.R = np.eye(n_dim_meas) * R
    high_ukf.x = np.array([high_train_data[0], 0])

    low_ukf = UnscentedKalmanFilter(dim_x=n_dim_state, dim_z=n_dim_meas, fx=low_fx, hx=low_hx, dt=dt, points=points)
    low_ukf.P = np.eye(n_dim_state) * P
    low_ukf.Q = Q_discrete_white_noise(dim=n_dim_state, dt=dt, var=0.004) * Q
    low_ukf.R = np.eye(n_dim_meas) * R
    low_ukf.x = np.array([low_train_data[0], 0])

    train_predictions = []
    for z in train_data:
        ukf.predict()
        train_predictions.append(ukf.x[0])
        ukf.update(z)

    test_predictions = []
    for z in test_data:
        ukf.predict()
        test_predictions.append(ukf.x[0])
        ukf.update(z)

    high_train_predictions = []
    for z in high_train_data:
        high_ukf.predict()
        high_train_predictions.append(high_ukf.x[0])
        high_ukf.update(z)

    high_test_predictions = []
    for z in high_test_data:
        high_ukf.predict()
        high_test_predictions.append(high_ukf.x[0])
        high_ukf.update(z)

    low_train_predictions = []
    for z in low_train_data:
        low_ukf.predict()
        low_train_predictions.append(low_ukf.x[0])
        low_ukf.update(z)

    low_test_predictions = []
    for z in low_test_data:
        low_ukf.predict()
        low_test_predictions.append(low_ukf.x[0])
        low_ukf.update(z)

    ukf.predict()
    high_ukf.predict()
    low_ukf.predict()

    train_mae = mean_absolute_error(train_data, train_predictions)
    train_mse = mean_squared_error(train_data, train_predictions)
    train_rmse = np.sqrt(train_mse)
    train_mape = mean_absolute_percentage_error(train_data, train_predictions)
    train_r2 = r2_score(train_data, train_predictions)

    test_mae = mean_absolute_error(test_data, test_predictions)
    test_mse = mean_squared_error(test_data, test_predictions)
    test_rmse = np.sqrt(test_mse)
    test_mape = mean_absolute_percentage_error(test_data, test_predictions)
    test_r2 = r2_score(test_data, test_predictions)

    high_train_mae = mean_absolute_error(high_train_data, high_train_predictions)
    high_train_mse = mean_squared_error(high_train_data, high_train_predictions)
    high_train_rmse = np.sqrt(high_train_mse)
    high_train_mape = mean_absolute_percentage_error(high_train_data, high_train_predictions)
    high_train_r2 = r2_score(high_train_data, high_train_predictions)

    high_test_mae = mean_absolute_error(high_test_data, high_test_predictions)
    high_test_mse = mean_squared_error(high_test_data, high_test_predictions)
    high_test_rmse = np.sqrt(high_test_mse)
    high_test_mape = mean_absolute_percentage_error(high_test_data, high_test_predictions)
    high_test_r2 = r2_score(high_test_data, high_test_predictions)

    low_train_mae = mean_absolute_error(low_train_data, low_train_predictions)
    low_train_mse = mean_squared_error(low_train_data, low_train_predictions)
    low_train_rmse = np.sqrt(low_train_mse)
    low_train_mape = mean_absolute_percentage_error(low_train_data, low_train_predictions)
    low_train_r2 = r2_score(low_train_data, low_train_predictions)

    low_test_mae = mean_absolute_error(low_test_data, low_test_predictions)
    low_test_mse = mean_squared_error(low_test_data, low_test_predictions)
    low_test_rmse = np.sqrt(low_test_mse)
    low_test_mape = mean_absolute_percentage_error(low_test_data, low_test_predictions)
    low_test_r2 = r2_score(low_test_data, low_test_predictions)

    metric_data = {
        "Metric": ["MAE", "RMSE", "MAPE", "R2"],
        "Train": [train_mae, train_rmse, train_mape, train_r2],
        "Test": [test_mae, test_rmse, test_mape, test_r2],
        "High Train": [high_train_mae, high_train_rmse, high_train_mape, high_train_r2],
        "High Test": [high_test_mae, high_test_rmse, high_test_mape, high_test_r2],
        "Low Train": [low_train_mae, low_train_rmse, low_train_mape, low_train_r2],
        "Low Test": [low_test_mae, low_test_rmse, low_test_mape, low_test_r2]
        }
    metric_df = pd.DataFrame(metric_data)
    print(f"{symb} METRICS for {frame} \n {metric_df}")
    return ukf, high_ukf, low_ukf
	
eth_symbols = ["ETH/USD"]
btc_symbols = ["BTC/USD"]

btc_ukf, btc_high_ukf, btc_low_ukf = ukf_factory("1H", btc_symbols)
eth_ukf, eth_high_ukf, eth_low_ukf = ukf_factory("1H", eth_symbols)

def calculate_metrics(trades, initial_capital, date_index):
    total_trades = len(trades)
    winning_trades = sum(1 for trade in trades if trade['profit'] > 0)
    losing_trades = sum(1 for trade in trades if trade['profit'] < 0)
    net_profit = sum(trade['profit'] for trade in trades if trade['profit'] > 0)
    net_loss = sum(trade['profit'] for trade in trades if trade['profit'] < 0)
    total_profit = net_profit - abs(net_loss)
    starting_capital = initial_capital
    ending_capital = initial_capital + total_profit
    returns = ending_capital/initial_capital -1 if initial_capital != 0 else 0
    win_rate = winning_trades / total_trades if total_trades > 0 else 0
    win_pct = win_rate * 100
    average_win = np.mean([trade['profit'] for trade in trades if trade['profit'] > 0]) if winning_trades > 0 else 0
    average_loss = np.mean([trade['profit'] for trade in trades if trade['profit'] < 0]) if losing_trades > 0 else 0
    max_drawdown = calculate_max_drawdown_combined(trades, initial_capital, date_index)
    drawdown_pct = max_drawdown * 100
    total_return_percentage = returns * 100
    duration = len(date_index) / 24

    daily_returns = []
    current_capital = initial_capital
    sorted_trades = sorted(trades, key=lambda x: x['entry_date'])

    trade_index = 0
    for date in date_index:
        while trade_index < len(sorted_trades) and sorted_trades[trade_index]['exit_date'] is not None and sorted_trades[trade_index]['exit_date'] <= date:
             current_capital += sorted_trades[trade_index]['profit']
             trade_index += 1

        daily_return = (current_capital - initial_capital)/initial_capital if initial_capital != 0 else 0
        daily_returns.append(daily_return)

    sharpe_ratio = calculate_sharpe_ratio(daily_returns) if len(daily_returns) > 1 else 0

    metrics = {
        "METRICS": ["Total Return(%)", "Total Trades", "Winning Trades", "Losing Trades", "Win Rate(%)", "Starting Capital", "Ending Capital", "Total Profit", "Average Win", "Average Loss", "Max Drawdown(%)", "Sharpe Ratio", "Duration(Days)"],
        "VALUES": [total_return_percentage, total_trades, winning_trades, losing_trades, win_pct, starting_capital, ending_capital, total_profit, average_win, average_loss, drawdown_pct, sharpe_ratio, duration]
    }
    metrics = pd.DataFrame(metrics)
    return metrics

def calculate_combined_metrics(trades, initial_capital, date_index, final_value):
    total_trades = len(trades)
    winning_trades = sum(1 for trade in trades if trade['profit'] > 0)
    losing_trades = sum(1 for trade in trades if trade['profit'] < 0)
    net_profit = sum(trade['profit'] for trade in trades if trade['profit'] > 0)
    net_loss = sum(trade['profit'] for trade in trades if trade['profit'] < 0)
    total_pnl = net_profit - abs(net_loss)
    starting_capital = initial_capital
    ending_capital = final_value
    total_profit = ending_capital - initial_capital
    commissions = (initial_capital + total_pnl) - final_value
    returns = ending_capital/initial_capital -1 if initial_capital != 0 else 0
    win_rate = winning_trades / total_trades if total_trades > 0 else 0
    win_pct = win_rate * 100
    average_win = np.mean([trade['profit'] for trade in trades if trade['profit'] > 0]) if winning_trades > 0 else 0
    average_loss = np.mean([trade['profit'] for trade in trades if trade['profit'] < 0]) if losing_trades > 0 else 0
    max_drawdown = calculate_max_drawdown_combined(trades, initial_capital, date_index)
    drawdown_pct = max_drawdown * 100
    total_return_percentage = returns * 100
    duration = len(date_index) / 24

    daily_returns = []
    current_capital = initial_capital
    sorted_trades = sorted(trades, key=lambda x: x['entry_date'])

    trade_index = 0
    for date in date_index:
        while trade_index < len(sorted_trades) and sorted_trades[trade_index]['exit_date'] is not None and sorted_trades[trade_index]['exit_date'] <= date:
             current_capital += sorted_trades[trade_index]['profit']
             trade_index += 1

        daily_return = (current_capital - initial_capital)/initial_capital if initial_capital != 0 else 0
        daily_returns.append(daily_return)

    sharpe_ratio = calculate_sharpe_ratio(daily_returns) if len(daily_returns) > 1 else 0

    metrics = {
        "METRICS": ["Total Return(%)", "Total Trades", "Winning Trades", "Losing Trades", "Win Rate(%)", "Starting Capital", "Ending Capital", "Realized PnL", "Total Commissions Charged", "Total PnL(+ Commisions)", "Average Win", "Average Loss", "Max Drawdown(%)", "Sharpe Ratio", "Duration(Days)"],
        "VALUES": [total_return_percentage, total_trades, winning_trades, losing_trades, win_pct, starting_capital, ending_capital, total_profit, commissions, total_pnl, average_win, average_loss, drawdown_pct, sharpe_ratio, duration]
    }
    metrics = pd.DataFrame(metrics)
    return metrics

def calculate_max_drawdown_combined(trades, initial_capital, date_index):
    peak = initial_capital
    max_drawdown = 0
    current_capital = initial_capital
    sorted_trades = sorted(trades, key=lambda x: x['entry_date'])
    trade_index = 0

    for date in date_index:
        while trade_index < len(sorted_trades) and sorted_trades[trade_index]['exit_date'] is not None and sorted_trades[trade_index]['exit_date'] <= date:
             current_capital += sorted_trades[trade_index]['profit']
             trade_index += 1

        if current_capital > peak:
            peak = current_capital
        drawdown = (peak - current_capital) / peak if peak != 0 else 0
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown

def visualize_trades(data, trades):
    plt.figure(figsize=(15, 7))
    plt.plot(data.index, data['close'], label='Close Price', color='blue')

    buy_entries = [trade['entry_date'] for trade in trades if trade['signal'] == 'BUY']
    buy_prices = [trade['entry_price'] for trade in trades if trade['signal'] == 'BUY']
    sell_entries = [trade['entry_date'] for trade in trades if trade['signal'] == 'SELL']
    sell_prices = [trade['entry_price'] for trade in trades if trade['signal'] == 'SELL']

    plt.scatter(buy_entries, buy_prices, marker='^', color='green', label='Buy Entry')
    plt.scatter(sell_entries, sell_prices, marker='v', color='red', label='Sell Entry')

    for trade in trades:
        if trade['exit_date']:
            plt.plot([trade['entry_date'], trade['exit_date']], [trade['entry_price'], trade['exit_price']], color='gray', linestyle='--')
            plt.scatter(trade['exit_date'], trade['exit_price'], marker='o', color='black')

    plt.xlabel('Date')
    plt.ylabel('Price')
    plt.title(f"Trades Visualization for {trades[0]['asset'] if trades else 'Asset'}") 
    plt.legend()
    plt.grid(True)
    plt.show()

def calculate_sharpe_ratio(returns, risk_free_rate=0.0025):
    if len(returns) < 2:
        return 0
    returns_array = np.array(returns)
    excess_returns = returns_array - risk_free_rate
    sharpe_ratio = np.mean(excess_returns) / np.std(excess_returns) * math.sqrt(365)  
    return sharpe_ratio
	
def extractor(input):
    input_str = str(input)
    third = input_str[2]
    fourth = input_str[3]
    goal = third + fourth
    return float(goal)

backtest_duration = 7
length = backtest_duration * 24

btc_valid  = pd.read_csv('btc_back366.csv')
btc_val = btc_valid[-length:]
print(btc_val)

eth_valid  = pd.read_csv('eth_back366.csv')
eth_val = eth_valid[-length:]
print(eth_val)


def backtest_ukf_viz_BTC_ETH_FlexibleAllocation_Aligned2(validation_btc, validation_eth, high_ukf_btc, low_ukf_btc, ukf_btc, high_ukf_eth, low_ukf_eth, ukf_eth, initial_capital=2, visualize=False):

    trades_btc = []
    trades_eth = []
    total_portfolio_capital = initial_capital
    leverage = 10
    
    positions_btc = 0
    entry_price_btc = 0
    in_trade_btc = False
    exit_trade_btc = False
    tp_price_btc = 0
    sl_price_btc = 0
    leverage_btc = leverage 

    positions_eth = 0
    entry_price_eth = 0
    in_trade_eth = False
    exit_trade_eth = False
    tp_price_eth = 0
    sl_price_eth = 0
    leverage_eth = leverage 

    market_price_btc = []
    market_high_btc = []
    market_low_btc = []
    pred_close_btc = []
    pred_high_btc = []
    pred_low_btc = []

    market_price_eth = []
    market_high_eth = []
    market_low_eth = []
    pred_close_eth = []
    pred_high_eth = []
    pred_low_eth = []
    
    part_port = False

    if len(validation_btc) > 1:
        market_price_btc.append(validation_btc['close'].iloc[1])
        market_high_btc.append(validation_btc['high'].iloc[1])
        market_low_btc.append(validation_btc['low'].iloc[1])
    else:
         print("Warning: BTC validation data has less than 2 rows, cannot initialize market price lists properly.")

    if len(validation_eth) > 1:
        market_price_eth.append(validation_eth['close'].iloc[1])
        market_high_eth.append(validation_eth['high'].iloc[1])
        market_low_eth.append(validation_eth['low'].iloc[1])
    else:
         print("Warning: ETH validation data has less than 2 rows, cannot initialize market price lists properly.")

    print("Starting backtest iteration loop (assuming aligned dataframes).")
    for index, row_btc in validation_btc.iterrows():
        indexx = index - 1 
        row_eth = validation_eth.loc[indexx]

        ## Process BTC ##
        if market_price_btc:
            prev_price_btc = market_price_btc[-1]
            prev_low_btc = market_low_btc[-1]
            prev_high_btc = market_high_btc[-1]
        else:
            prev_price_btc = row_btc['close']
            prev_low_btc = row_btc['low']
            prev_high_btc = row_btc['high']

        market_price_btc.append(row_btc['close'])
        market_low_btc.append(row_btc['low'])
        market_high_btc.append(row_btc['high'])

        price_btc = row_btc['close']
        high_btc = row_btc['high']
        low_btc = row_btc['low']

        high_ukf_btc.predict()
        high_pred_btc = high_ukf_btc.x[0]
        pred_high_btc.append(high_pred_btc)
        high_ukf_btc.update(high_btc)

        low_ukf_btc.predict()
        low_pred_btc = low_ukf_btc.x[0]
        pred_low_btc.append(low_pred_btc)
        low_ukf_btc.update(low_btc)

        ukf_btc.predict()
        pred_btc = ukf_btc.x[0]
        pred_close_btc.append(pred_btc)
        ukf_btc.update(price_btc)

        signal_btc = None
        entry_price_candidate_btc = None
        tp_price_candidate_btc = None

        if pred_btc > price_btc:
            trend_btc = "up"
            pct_diff_btc = abs(1 - (pred_btc / price_btc))
            vol_btc = abs(1 - (high_pred_btc / pred_btc))
        elif price_btc > pred_btc:
            trend_btc = "down"
            pct_diff_btc = abs(1 - (price_btc / pred_btc))
            vol_btc = abs(1 - (pred_btc / low_pred_btc))
        else:
            pct_diff_btc = 0
            vol_btc = 0

        btc_buy_tp = 1.0075
        btc_sell_tp = 2 - btc_buy_tp
        stake = 0.9975
        rr_btc = 8.6 
        strict_btc = 0.0045 
        stricts_btc = 0.006 

        if pct_diff_btc <= stricts_btc or vol_btc >= stricts_btc: 
            if trend_btc == "up":
                buy_price_candidate_btc = low_pred_btc
                tp_price_candidate_btc = pred_btc
                prof_btc = abs(1 - (tp_price_candidate_btc / buy_price_candidate_btc))

                if tp_price_candidate_btc > buy_price_candidate_btc and prof_btc >= strict_btc:
                    if buy_price_candidate_btc >= row_btc['low'] and buy_price_candidate_btc <= row_btc['high']:
                        signal_btc = "BUY"
                        entry_price_candidate_btc = buy_price_candidate_btc
                        tp_candidate_btc = entry_price_candidate_btc * btc_buy_tp
                        if tp_price_candidate_btc > tp_candidate_btc:
                            tp_price_candidate_btc = tp_candidate_btc

            elif trend_btc == "down":
                sell_price_candidate_btc = high_pred_btc
                tp_price_candidate_btc = pred_btc
                prof_btc = abs(1 - (sell_price_candidate_btc / tp_price_candidate_btc))

                if sell_price_candidate_btc > tp_price_candidate_btc and prof_btc >= strict_btc:
                    if sell_price_candidate_btc >= row_btc['low'] and sell_price_candidate_btc <= row_btc['high']:
                        signal_btc = "SELL"
                        entry_price_candidate_btc = sell_price_candidate_btc
                        tp_candidate_btc = entry_price_candidate_btc * btc_buy_tp
                        if tp_price_candidate_btc > tp_candidate_btc:
                            tp_price_candidate_btc = tp_candidate_btc


        ## Process ETH ##
        if market_price_eth:
            prev_price_eth = market_price_eth[-1]
            prev_low_eth = market_low_eth[-1]
            prev_high_eth = market_high_eth[-1]
        else:
            prev_price_eth = row_eth['close']
            prev_low_eth = row_eth['low']
            prev_high_eth = row_eth['high']

        market_price_eth.append(row_eth['close'])
        market_low_eth.append(row_eth['low'])
        market_high_eth.append(row_eth['high'])

        price_eth = row_eth['close']
        high_eth = row_eth['high']
        low_eth = row_eth['low']

        high_ukf_eth.predict()
        high_pred_eth = high_ukf_eth.x[0]
        pred_high_eth.append(high_pred_eth)
        high_ukf_eth.update(high_eth)

        low_ukf_eth.predict()
        low_pred_eth = low_ukf_eth.x[0]
        pred_low_eth.append(low_pred_eth)
        low_ukf_eth.update(low_eth)

        ukf_eth.predict()
        pred_eth = ukf_eth.x[0]
        pred_close_eth.append(pred_eth)
        ukf_eth.update(price_eth)

        signal_eth = None
        entry_price_candidate_eth = None
        tp_price_candidate_eth = None

        if pred_eth > price_eth:
            trend_eth = "up"
            pct_diff_eth = abs(1 - (pred_eth / price_eth))
            vol_eth = abs(1 - (high_pred_eth / pred_eth))
        elif price_eth > pred_eth:
            trend_eth = "down"
            pct_diff_eth = abs(1 - (price_eth / pred_eth))
            vol_eth = abs(1 - (pred_eth / low_pred_eth))
        else:
            pct_diff_eth = 0
            vol_eth = 0

        eth_buy_tp = 1.0085
        eth_sell_tp = 2 - eth_buy_tp
        rr_eth = 9.6 
        strict_eth = 0.0035 
        stricts_eth = 0.008 

        if pct_diff_eth <= stricts_eth or vol_eth >= stricts_eth: 
            if trend_eth == "up":
                buy_price_candidate_eth = low_pred_eth
                tp_price_candidate_eth = pred_eth
                prof_eth = abs(1 - (tp_price_candidate_eth / buy_price_candidate_eth))

                if tp_price_candidate_eth > buy_price_candidate_eth and prof_eth >= strict_eth:
                    if buy_price_candidate_eth >= row_eth['low'] and buy_price_candidate_eth <= row_eth['high']:
                        signal_eth = "BUY"
                        entry_price_candidate_eth = buy_price_candidate_eth
                        tp_candidate_eth = entry_price_candidate_eth * eth_buy_tp
                        if tp_price_candidate_eth > tp_candidate_eth:
                            tp_price_candidate_eth = tp_candidate_eth

            elif trend_eth == "down":
                sell_price_candidate_eth = high_pred_eth
                tp_price_candidate_eth = pred_eth
                prof_eth = abs(1 - (sell_price_candidate_eth / tp_price_candidate_eth))

                if sell_price_candidate_eth > tp_price_candidate_eth and prof_eth >= strict_eth:
                    if sell_price_candidate_eth >= row_eth['low'] and sell_price_candidate_eth <= row_eth['high']:
                        signal_eth = "SELL"
                        entry_price_candidate_eth = sell_price_candidate_eth
                        tp_candidate_eth = entry_price_candidate_eth * eth_sell_tp
                        if tp_price_candidate_eth < tp_candidate_eth:
                            tp_price_candidate_eth = tp_candidate_eth

        
        ## Portfolio Allocation and Trade Execution ##
        
        Max_Loss = total_portfolio_capital * 16
        btc_Max_Loss = (total_portfolio_capital * 8.5) * 32.5 
        stop_lossed = False
        
        if signal_btc and signal_eth and not in_trade_btc and not in_trade_eth:
            # Signal from both, split capital allocation intent
            capital_btc = total_portfolio_capital / 2
            capital_eth = total_portfolio_capital / 2

            # Execute BTC Trade
            trigger_price_btc = entry_price_candidate_btc
            tp_price_btc = tp_price_candidate_btc
            entry_price_btc, positions_btc, commission_fees_btc, _btc_valid = \
                apply_realistic_entry(trigger_price_btc, signal_btc,
                                       capital_btc, leverage_btc, BTC_LOT_STEP)

            if not _btc_valid:
                print(f"{row_btc['timestamp']}: BTC signal REJECTED — "
                      f"position size after rounding fails minimum notional "
                      f"(${MIN_NOTIONAL}). No trade placed.")
                in_trade_btc = False
            else:
                # Deduct true margin used + fee from running capital
                margin_used_btc = (positions_btc * entry_price_btc) / leverage_btc
                total_portfolio_capital -= margin_used_btc
                total_portfolio_capital -= commission_fees_btc
                
                # Correct absolute stop-loss math based on positions
                price_delta = btc_Max_Loss / positions_btc
                sl_price_btc = entry_price_btc - price_delta if signal_btc == 'BUY' else entry_price_btc + price_delta
                
                in_trade_btc = True
                trades_btc.append({
                    'asset': 'BTC',
                    'entry_date': row_btc.name,
                    'entry_price': entry_price_btc,
                    'trigger_price': trigger_price_btc,
                    'entry_timestamp': row_btc['timestamp'],
                    'signal': signal_btc,
                    'exit_date': None,
                    'exit_price': None,
                    'profit': 0,
                    'tp_price': tp_price_btc,
                    'sl_price': sl_price_btc,
                    'margin_used': margin_used_btc # Track margin to return at exit
                })
                print(f"{row_btc['timestamp']}: Both signals. Entered BTC {signal_btc} at {entry_price_btc:.2f} "
                      f"(trigger was {trigger_price_btc:.2f}), tp @ {tp_price_btc:.2f}, sl @ {sl_price_btc:.2f} "
                      f"allocating margin {margin_used_btc:.2f}")


            # Execute ETH Trade
            trigger_price_eth = entry_price_candidate_eth
            tp_price_eth = tp_price_candidate_eth
            entry_price_eth, positions_eth, commission_fees_eth, _eth_valid = \
                apply_realistic_entry(trigger_price_eth, signal_eth,
                                       capital_eth, leverage_eth, ETH_LOT_STEP)

            if not _eth_valid:
                print(f"{row_eth['timestamp']}: ETH signal REJECTED — "
                      f"position size after rounding fails minimum notional "
                      f"(${MIN_NOTIONAL}). No trade placed.")
                in_trade_eth = False
            else:
                margin_used_eth = (positions_eth * entry_price_eth) / leverage_eth
                total_portfolio_capital -= margin_used_eth
                total_portfolio_capital -= commission_fees_eth
                
                price_delta = Max_Loss / positions_eth
                sl_price_eth = entry_price_eth - price_delta if signal_eth == 'BUY' else entry_price_eth + price_delta
                
                in_trade_eth = True
                trades_eth.append({
                    'asset': 'ETH',
                    'entry_date': row_eth.name,
                    'entry_price': entry_price_eth,
                    'trigger_price': trigger_price_eth,
                    'entry_timestamp': row_eth['timestamp'],
                    'signal': signal_eth,
                    'exit_date': None,
                    'exit_price': None,
                    'profit': 0,
                    'tp_price': tp_price_eth,
                    'sl_price': sl_price_eth,
                    'margin_used': margin_used_eth
                })
                print(f"{row_eth['timestamp']}: Both signals. Entered ETH {signal_eth} at {entry_price_eth:.2f} "
                      f"(trigger was {trigger_price_eth:.2f}), tp @ {tp_price_eth:.2f}, sl @ {sl_price_eth:.2f} "
                      f"allocating margin {margin_used_eth:.2f}")
            
            part_port = True


        elif signal_btc and not signal_eth and not in_trade_btc and not in_trade_eth:
            capital_btc = total_portfolio_capital

            trigger_price_btc = entry_price_candidate_btc
            tp_price_btc = tp_price_candidate_btc
            entry_price_btc, positions_btc, commission_fees_btc, _btc_valid = \
                apply_realistic_entry(trigger_price_btc, signal_btc,
                                       capital_btc, leverage_btc, BTC_LOT_STEP)

            if not _btc_valid:
                print(f"{row_btc['timestamp']}: BTC signal REJECTED — "
                      f"position size after rounding fails minimum notional "
                      f"(${MIN_NOTIONAL}). No trade placed.")
                in_trade_btc = False
            else:
                margin_used_btc = (positions_btc * entry_price_btc) / leverage_btc
                total_portfolio_capital -= margin_used_btc
                total_portfolio_capital -= commission_fees_btc
                
                price_delta = btc_Max_Loss / positions_btc
                sl_price_btc = entry_price_btc - price_delta if signal_btc == 'BUY' else entry_price_btc + price_delta
                
                in_trade_btc = True
                trades_btc.append({
                    'asset': 'BTC',
                    'entry_date': row_btc.name,
                    'entry_price': entry_price_btc,
                    'trigger_price': trigger_price_btc,
                    'entry_timestamp': row_btc['timestamp'],
                    'signal': signal_btc,
                    'exit_date': None,
                    'exit_price': None,
                    'profit': 0,
                    'tp_price': tp_price_btc,
                    'sl_price': sl_price_btc,
                    'margin_used': margin_used_btc
                })
                print(f"{row_btc['timestamp']}: BTC signal only. Entered BTC {signal_btc} at {entry_price_btc:.2f} "
                      f"(trigger was {trigger_price_btc:.2f}), tp @ {tp_price_btc:.2f}, sl @ {sl_price_btc:.2f} "
                      f"allocating margin {margin_used_btc:.2f}")


        elif not signal_btc and signal_eth and not in_trade_btc and not in_trade_eth:
            capital_eth = total_portfolio_capital

            trigger_price_eth = entry_price_candidate_eth
            tp_price_eth = tp_price_candidate_eth
            entry_price_eth, positions_eth, commission_fees_eth, _eth_valid = \
                apply_realistic_entry(trigger_price_eth, signal_eth,
                                       capital_eth, leverage_eth, ETH_LOT_STEP)

            if not _eth_valid:
                print(f"{row_eth['timestamp']}: ETH signal REJECTED — "
                      f"position size after rounding fails minimum notional "
                      f"(${MIN_NOTIONAL}). No trade placed.")
                in_trade_eth = False
            else:
                margin_used_eth = (positions_eth * entry_price_eth) / leverage_eth
                total_portfolio_capital -= margin_used_eth
                total_portfolio_capital -= commission_fees_eth
                
                price_delta = Max_Loss / positions_eth
                sl_price_eth = entry_price_eth - price_delta if signal_eth == 'BUY' else entry_price_eth + price_delta
                
                in_trade_eth = True
                trades_eth.append({
                    'asset': 'ETH',
                    'entry_date': row_eth.name,
                    'entry_price': entry_price_eth,
                    'trigger_price': trigger_price_eth,
                    'entry_timestamp': row_eth['timestamp'],
                    'signal': signal_eth,
                    'exit_date': None,
                    'exit_price': None,
                    'profit': 0,
                    'tp_price': tp_price_eth,
                    'sl_price': sl_price_eth,
                    'margin_used': margin_used_eth
                })
                print(f"{row_eth['timestamp']}: ETH signal only. Entered ETH {signal_eth} at {entry_price_eth:.2f} "
                      f"(trigger was {trigger_price_eth:.2f}), tp @ {tp_price_eth:.2f}, sl @ {sl_price_eth:.2f} "
                      f"allocating margin {margin_used_eth:.2f}")


        # First, handle exits for any Open trades
        # BTC Exit Logic
        if in_trade_btc:
            latest_trade_btc = trades_btc[-1]
            exit_trade_btc = False
            exit_price_btc = None
            exit_leg_btc = None
            entry_price_btc = latest_trade_btc['entry_price']
            
            # Drawdown logic without artificial leverage multiplier
            net_profit = (row_btc['high'] - latest_trade_btc['entry_price']) * positions_btc if latest_trade_btc['signal'] == 'BUY' else (latest_trade_btc['entry_price'] - row_btc['low']) * positions_btc
            net_drawdown = (row_btc['low'] - latest_trade_btc['entry_price']) * positions_btc if latest_trade_btc['signal'] == 'BUY' else (latest_trade_btc['entry_price'] - row_btc['high']) * positions_btc
            drawdown_price = row_btc['high'] if latest_trade_btc['signal'] == 'SELL' else row_btc['low']
            
            # Liquidation happens if you lose your required initial margin
            initial_margin_btc = latest_trade_btc['margin_used']
            
            print(f"{row_btc['timestamp']}: Net Profit: {net_profit} \n Net Drawdown: {net_drawdown}, Drawdown Price: {drawdown_price}")
            
            if latest_trade_btc['signal'] == 'BUY':
                if row_btc['low'] <= latest_trade_btc['sl_price']: # Stop Loss
                    exit_trade_btc = True
                    exit_price_btc = latest_trade_btc['sl_price']
                    exit_leg_btc = 'SL'
                    if net_drawdown < 0 and abs(net_drawdown) >= initial_margin_btc:
                        print("--------------------------------------------------Account_liquidated----------------------------------------------------------")
                elif net_profit >= (initial_margin_btc * 0.225):
                    print("BTC Net Profit reached 7.5% of margin, exiting trade")
                    exit_trade_btc = True
                    if row_btc['high'] >= latest_trade_btc['tp_price']:
                        exit_price_btc = latest_trade_btc['tp_price']
                        exit_leg_btc = 'TP'
                    else:
                        exit_price_btc = price_btc 
                        exit_leg_btc = 'MARKET'
                elif row_btc['high'] >= latest_trade_btc['tp_price']: # Take Profit
                    exit_trade_btc = True
                    exit_price_btc = latest_trade_btc['tp_price']
                    exit_leg_btc = 'TP'
                
                if not exit_trade_btc:
                    if trend_btc == "down" and pct_diff_btc >= 0.00005: 
                        exit_trade_btc = True
                        exit_price_btc = price_btc 
                        exit_leg_btc = 'MARKET'
                        print("Signal for reversal Triggered")
                    
            elif latest_trade_btc['signal'] == 'SELL':
                if row_btc['high'] >= latest_trade_btc['sl_price']: # Stop Loss
                    exit_trade_btc = True
                    exit_price_btc = latest_trade_btc['sl_price']
                    exit_leg_btc = 'SL'
                    if net_drawdown < 0 and abs(net_drawdown) >= initial_margin_btc:
                        print("---------------------------------------------------Account_liquidated---------------------------------------------------------")
                elif net_profit >= (initial_margin_btc * 0.225):
                    print("BTC Net Profit reached 7.5% of margin, exiting trade")
                    exit_trade_btc = True
                    if row_btc['low'] <= latest_trade_btc['tp_price']:
                        exit_price_btc = latest_trade_btc['tp_price']
                        exit_leg_btc = 'TP'
                    else:
                        exit_price_btc = price_btc
                        exit_leg_btc = 'MARKET'
                        
                elif row_btc['low'] <= latest_trade_btc['tp_price']: # Take Profit
                    exit_trade_btc = True
                    exit_price_btc = latest_trade_btc['tp_price']
                    exit_leg_btc = 'TP'
                if not exit_trade_btc:
                    if trend_btc == "down" and pct_diff_btc >= 0.00005: 
                        exit_trade_btc = True
                        exit_price_btc = price_btc 
                        exit_leg_btc = 'MARKET'
                        print("Signal for reversal Triggered")
            
            if exit_trade_btc:
                if exit_leg_btc in ('SL', 'TP'):
                    exit_price_btc, exit_commission_fee = apply_realistic_exit(
                        exit_price_btc, exit_leg_btc, latest_trade_btc['signal'], positions_btc
                    )
                else:
                    exit_commission_fee = TAKER_FEE * (positions_btc * exit_price_btc)

                funding_cost = compute_funding_cost(
                    latest_trade_btc.get('entry_timestamp'), row_btc['timestamp'],
                    positions_btc, entry_price_btc
                )

                profit_btc = (exit_price_btc - latest_trade_btc['entry_price']) * positions_btc if latest_trade_btc['signal'] == 'BUY' else (latest_trade_btc['entry_price'] - exit_price_btc) * positions_btc
                profit_btc -= exit_commission_fee
                profit_btc -= funding_cost
                
                # Restore locked margin plus true profit
                total_portfolio_capital += (initial_margin_btc + profit_btc)
                positions_btc = 0
                in_trade_btc = False
                latest_trade_btc['exit_date'] = row_btc.name
                latest_trade_btc['exit_price'] = exit_price_btc
                latest_trade_btc['exit_leg'] = exit_leg_btc
                latest_trade_btc['funding_cost'] = funding_cost
                latest_trade_btc['exit_commission'] = exit_commission_fee
                latest_trade_btc['profit'] = profit_btc
                profit_or_loss = "Profit" if latest_trade_btc['profit'] > 0 else "Loss"

                print(f"{row_btc['timestamp']}: Exited BTC {latest_trade_btc['signal']} trade ({exit_leg_btc}) with "
                      f"{profit_or_loss} {profit_btc:.2f} (funding: -{funding_cost:.4f}, exit fee: -{exit_commission_fee:.4f}), "
                      f"capital {total_portfolio_capital:.2f}")


        # ETH Exit Logic
        if in_trade_eth:
            latest_trade_eth = trades_eth[-1]
            exit_trade_eth = False
            exit_price_eth = None
            exit_leg_eth = None
            entry_price_eth = latest_trade_eth['entry_price']
            
            # Drawdown logic without artificial leverage multiplier
            net_profit = (row_eth['high'] - latest_trade_eth['entry_price']) * positions_eth if latest_trade_eth['signal'] == 'BUY' else (latest_trade_eth['entry_price'] - row_eth['low']) * positions_eth
            net_drawdown = (row_eth['low'] - latest_trade_eth['entry_price']) * positions_eth if latest_trade_eth['signal'] == 'BUY' else (latest_trade_eth['entry_price'] - row_eth['high']) * positions_eth
            drawdown_price = row_eth['high'] if latest_trade_eth['signal'] == 'SELL' else row_eth['low']
            
            initial_margin_eth = latest_trade_eth['margin_used']
            
            print(f"{row_eth['timestamp']}: Net Profit: {net_profit} \n Net Drawdown: {net_drawdown}, DrawDown Price: {drawdown_price}")
            
            if latest_trade_eth['signal'] == 'BUY':
                
                if row_eth['low'] <= latest_trade_eth['sl_price']: # Stop Loss
                    exit_trade_eth = True
                    exit_price_eth = latest_trade_eth['sl_price']
                    exit_leg_eth = 'SL'
                    if net_drawdown < 0 and abs(net_drawdown) >= initial_margin_eth:
                        print("---------------------------------------------------Account_liquidated---------------------------------------------------------")
                elif net_profit >= (initial_margin_eth * 0.175):
                    print("Net Profit reached 5% of margin, exiting trade")
                    exit_trade_eth = True
                    if row_eth['high'] >= latest_trade_eth['tp_price']:
                        exit_price_eth = latest_trade_eth['tp_price']
                        exit_leg_eth = 'TP'
                    else:
                        exit_price_eth = price_eth 
                        exit_leg_eth = 'MARKET'

                elif row_eth['high'] >= latest_trade_eth['tp_price']: # Take Profit
                    exit_trade_eth = True
                    exit_price_eth = latest_trade_eth['tp_price']
                    exit_leg_eth = 'TP'
                
                if not exit_trade_eth:
                    if trend_eth == "down" and pct_diff_eth >= 0.00005: 
                        exit_trade_eth = True
                        exit_price_eth = price_eth 
                        exit_leg_eth = 'MARKET'
                        print("Signal for reversal Triggered")
            
            elif latest_trade_eth['signal'] == 'SELL':
                
                if row_eth['high'] >= latest_trade_eth['sl_price']: # Stop Loss
                    exit_trade_eth = True
                    exit_price_eth = latest_trade_eth['sl_price']
                    exit_leg_eth = 'SL'
                    if net_drawdown < 0 and abs(net_drawdown) >= initial_margin_eth:
                        print("-----------------------------------------------Account_liquidated-------------------------------------------------------------")
                elif net_profit >= (initial_margin_eth * 0.175):
                    print("Net Profit reached 5% of margin, exiting trade")
                    exit_trade_eth = True
                    if row_eth['low'] <= latest_trade_eth['tp_price']:
                        exit_price_eth = latest_trade_eth['tp_price']
                        exit_leg_eth = 'TP'
                    else:
                        exit_price_eth = price_eth 
                        exit_leg_eth = 'MARKET'

                elif row_eth['low'] <= latest_trade_eth['tp_price']: # Take Profit
                    exit_trade_eth = True
                    exit_price_eth = latest_trade_eth['tp_price']
                    exit_leg_eth = 'TP'
                
                if not exit_trade_eth:
                    if trend_eth == "down" and pct_diff_eth >= 0.00005: 
                        exit_trade_eth = True
                        exit_price_eth = price_eth 
                        exit_leg_eth = 'MARKET'
                        print("Signal for reversal Triggered")

            if exit_trade_eth:
                if exit_leg_eth in ('SL', 'TP'):
                    exit_price_eth, exit_commission_fee = apply_realistic_exit(
                        exit_price_eth, exit_leg_eth, latest_trade_eth['signal'], positions_eth
                    )
                else:
                    exit_commission_fee = TAKER_FEE * (positions_eth * exit_price_eth)

                funding_cost = compute_funding_cost(
                    latest_trade_eth.get('entry_timestamp'), row_eth['timestamp'],
                    positions_eth, entry_price_eth
                )

                profit_eth = (exit_price_eth - latest_trade_eth['entry_price']) * positions_eth if latest_trade_eth['signal'] == 'BUY' else (latest_trade_eth['entry_price'] - exit_price_eth) * positions_eth
                profit_eth -= exit_commission_fee
                profit_eth -= funding_cost
                
                total_portfolio_capital += (initial_margin_eth + profit_eth)
                positions_eth = 0
                in_trade_eth = False
                latest_trade_eth['exit_date'] = row_eth.name
                latest_trade_eth['exit_price'] = exit_price_eth
                latest_trade_eth['exit_leg'] = exit_leg_eth
                latest_trade_eth['funding_cost'] = funding_cost
                latest_trade_eth['exit_commission'] = exit_commission_fee
                latest_trade_eth['profit'] = profit_eth
                profit_or_loss = "Profit" if latest_trade_eth['profit'] > 0 else "Loss"

                print(f"{row_eth['timestamp']}: Exited ETH {latest_trade_eth['signal']} trade ({exit_leg_eth}) with "
                      f"{profit_or_loss} {profit_eth:.2f} (funding: -{funding_cost:.4f}, exit fee: -{exit_commission_fee:.4f}), "
                      f"capital {total_portfolio_capital:.2f}")

    all_trades = trades_btc + trades_eth

    final_portfolio_value = total_portfolio_capital
    if in_trade_btc:
        final_portfolio_value += positions_btc * market_price_btc[-1] if market_price_btc else 0
    if in_trade_eth:
        final_portfolio_value += positions_eth * market_price_eth[-1] if market_price_eth else 0


    btc_results = calculate_metrics(trades_btc, initial_capital, validation_btc.index)
    eth_results = calculate_metrics(trades_eth, initial_capital, validation_eth.index)
    results = calculate_combined_metrics(all_trades, initial_capital, validation_btc.index, final_portfolio_value)
    
    print(f"BTC results, \n {btc_results} \n ETH results, \n {eth_results}")

    if visualize:
        if trades_btc:
            print("Visualizing BTC Trades:")
            visualize_trades(validation_btc, trades_btc)
        if trades_eth:
            print("\nVisualizing ETH Trades:")
            visualize_trades(validation_eth, trades_eth)

    return results
	
results = backtest_ukf_viz_BTC_ETH_FlexibleAllocation_Aligned2(btc_val, eth_val, btc_high_ukf, btc_low_ukf, btc_ukf, eth_high_ukf, eth_low_ukf, eth_ukf, initial_capital=10, visualize=False)
print(f"Total Backtest Metrics results, \n {results}")