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
from datetime import datetime
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

import time
# !pip install nest_asyncio
import nest_asyncio
nest_asyncio.apply()

import requests
import time
import hashlib
import hmac
import uuid
import time



from filterpy.kalman import UnscentedKalmanFilter
from filterpy.kalman import MerweScaledSigmaPoints
from filterpy.common import Q_discrete_white_noise

# ══════════════════════════════════════════════════════════════════════════
# REALISM UPGRADE — additions only, no changes to prediction/signal logic
# ══════════════════════════════════════════════════════════════════════════
#
# Everything below this block models real Binance USDⓈ-M Futures execution
# costs and constraints that the original backtest did not account for.
# The pessimistic SL-first-on-ambiguous-candle exit resolution further down
# in the main loop is left completely untouched — these additions only make
# the COST side of every trade more realistic, they do not change which
# trades win or lose on an ambiguous candle.

# Binance USDⓈ-M Futures fee schedule (VIP 0 / default tier, as of 2026).
# Entries in this system are LIMIT orders → maker fee.
# TP/SL exits are STOP_MARKET / TAKE_PROFIT_MARKET → always taker fee,
# because conditional market orders never add liquidity to the book.
MAKER_FEE = 0.0002    # 0.02% — limit entry
TAKER_FEE = 0.0005    # 0.05% — stop-market / take-profit-market exit

# Funding rate — perpetual futures charge this every 8 hours to whoever is
# holding a position at the funding timestamp. Using a representative
# long-run average for BTC/ETH perpetuals. Real-time rates swing between
# negative and positive, but the long-run average is a fair backtest proxy.
FUNDING_RATE_PER_8H = 0.0001   # 0.01% per 8-hour funding interval

# Slippage models. STOP_MARKET and TAKE_PROFIT_MARKET orders fill at the
# best available price once triggered — not at the exact trigger price.
# SL orders disproportionately trigger during fast, volatile moves (that's
# why the SL was hit), so they get a larger slippage allowance than TP fills,
# which more often trigger during calmer directional moves. LIMIT entries
# also slip slightly due to queue position — the order may partially fill
# at worse prices as the book moves through the limit level.
ENTRY_SLIPPAGE_PCT    = 0.0003   # 0.03% — LIMIT entry queue slippage
TP_EXIT_SLIPPAGE_PCT  = 0.0004   # 0.04% — TAKE_PROFIT_MARKET fill slippage
SL_EXIT_SLIPPAGE_PCT  = 0.0008   # 0.08% — STOP_MARKET fill slippage (worse: fast moves)

# Binance Futures minimum notional and lot-size step constraints.
# A signal that can't clear these in live trading would never actually
# place an order — the backtest should reflect the same constraint.
MIN_NOTIONAL = 5.0      # USDT, Binance Futures floor for BTCUSDT/ETHUSDT
BTC_LOT_STEP = 0.001
ETH_LOT_STEP = 0.001


def apply_realistic_entry(entry_price_candidate: float, side: str, capital: float,
                           leverage_val: float, lot_step: float):
    """
    Models a real LIMIT order fill: applies entry slippage, rounds the
    resulting position size to the exchange's lot step, and checks the
    Binance minimum notional floor.

    Returns:
        (filled_price, position_size, commission_fee, is_valid)
        is_valid=False means this trade would have been rejected or
        never placed live (notional too small / zero size after rounding).
    """
    if side == "BUY":
        filled_price = entry_price_candidate * (1 + ENTRY_SLIPPAGE_PCT)
    else:
        filled_price = entry_price_candidate * (1 - ENTRY_SLIPPAGE_PCT)

    raw_qty       = capital / filled_price
    position_size = math.floor(raw_qty / lot_step) * lot_step
    notional      = position_size * filled_price * leverage_val
    is_valid      = position_size > 0 and notional >= MIN_NOTIONAL
    commission_fee = MAKER_FEE * position_size

    return filled_price, position_size, commission_fee, is_valid


def apply_realistic_exit(trigger_price: float, leg: str, side: str,
                          position_size: float):
    """
    Models a real STOP_MARKET (leg='SL') or TAKE_PROFIT_MARKET (leg='TP')
    fill: applies exit slippage in the unfavorable direction and computes
    the taker commission charged on close.

    The direction of slippage is always against the trader:
      BUY position closing  → fills LOWER than the trigger price
      SELL position closing → fills HIGHER than the trigger price
    """
    slip_pct = SL_EXIT_SLIPPAGE_PCT if leg == "SL" else TP_EXIT_SLIPPAGE_PCT

    if side == "BUY":
        filled_price = trigger_price * (1 - slip_pct)
    else:
        filled_price = trigger_price * (1 + slip_pct)

    commission_fee = TAKER_FEE * position_size
    return filled_price, commission_fee


def compute_funding_cost(entry_ts, exit_ts, position_size: float,
                          entry_price: float, leverage_val: float) -> float:
    """
    Computes the total funding charge accrued over the life of a position.

    Binance charges funding on the full leveraged NOTIONAL value, every
    8 hours, to whoever is holding the position at the funding timestamp
    (00:00, 08:00, 16:00 UTC). Only complete 8-hour periods that were
    crossed during the hold are charged — a trade open for 7 hours 59
    minutes pays nothing; one that crosses exactly one funding timestamp
    pays one period.
    """
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

    notional = position_size * entry_price * leverage_val
    return funding_periods * FUNDING_RATE_PER_8H * notional


def round_to_lot(qty: float, lot_step: float) -> float:
    """Floors a quantity to the exchange's lot-size step, never rounds up."""
    return math.floor(qty / lot_step) * lot_step

# ══════════════════════════════════════════════════════════════════════════
# END REALISM UPGRADE BLOCK — original backtest logic continues below
# ══════════════════════════════════════════════════════════════════════════

def ukf_factory(timeframe, or_symbols): # FUNCTION FOR CREATING AND TRAINING UKFs
    global symb
    # UKF setup
    dt = 1   # Assuming daily interval (or adjust as per your data frequency)
    n_dim_state = 2  # price and velocity
    n_dim_meas = 1  # only measuring price
    frame = timeframe

    ukf_days = 30
    
    ukf_rows = ukf_days * 24
    print(or_symbols)
    if or_symbols == ['BTC/USD']: #"BTC/USD":
        symb = "BTC"
        crypto_bars_d =pd.read_csv('btc_back365.csv')
        crypto_bars_df = crypto_bars_d[:ukf_rows]
    elif or_symbols == ['ETH/USD']: #"ETH/USD":
        symb = "ETH"
        crypto_bars_d =pd.read_csv('eth_back365.csv')
        crypto_bars_df = crypto_bars_d[:ukf_rows]
    else:
        print("Error with getting symb for or_symbols", )
        # continue
        
    # crypto_bars_d =pd.read_csv('btc_back90.csv')
    # crypto_bars_df = crypto_bars_d[:720]

    if frame == "1H":
        data = crypto_bars_df# .copy()
    elif frame == "15T":
        agg_crypto_bars = aggregate_ohlcv_data(crypto_bars_df.copy(), aggregation_minutes=15)
        data = agg_crypto_bars.copy()
        data.dropna(inplace=True)
        # print(data.isnull().sum(axis=0))
    elif frame == "5T":
        agg_crypto_bars = aggregate_ohlcv_data(crypto_bars_df.copy(), aggregation_minutes=5)
        data = agg_crypto_bars.copy()
        data.dropna(inplace=True)

    # Define the state transition function
    def fx(x, dt):
        return np.array([x[0] + dt * x[1], x[1]])

    # Define the measurement function
    def hx(x):
        return np.array([x[0]])

    def high_fx(x, dt):
        return np.array([x[0] + dt * x[1], x[1]])

    # Define the measurement function
    def high_hx(x):
        return np.array([x[0]])

    def low_fx(x, dt):
        return np.array([x[0] + dt * x[1], x[1]])

    # Define the measurement function
    def low_hx(x):
        return np.array([x[0]])


    close_prices = data['close'].values
    high_prices = data['high'].values
    low_prices = data['low'].values
    var = (close_prices.std()) ** 2

    # Split the data into training and test sets
    train_size = int(len(close_prices) * 0.7)
    train_data, test_data = close_prices[:train_size], close_prices[train_size:]
    high_train_data, high_test_data = high_prices[:train_size], high_prices[train_size:]
    low_train_data, low_test_data = low_prices[:train_size], low_prices[train_size:]

    # Assuming you have found the best parameters from the grid search
    if symb == "ETH":
        best_params = {'alpha': 0.001, 'beta': 4.0, 'kappa': 1, 'P': 0.1, 'Q': 1.0, 'R': 0.01}
    elif symb == "BTC":
        best_params = {'alpha': 0.001, 'beta': 7.0, 'kappa': 0, 'P': 0.001, 'Q': 1.0, 'R': 0.01}

    # Initialize the UKF with the best parameters
    alpha, beta, kappa = best_params['alpha'], best_params['beta'], best_params['kappa']

    P, Q, R = best_params['P'], best_params['Q'], best_params['R']
    points = MerweScaledSigmaPoints(n=n_dim_state, alpha=alpha, beta=beta, kappa=kappa)

    # Close UKF
    ukf = UnscentedKalmanFilter(dim_x=n_dim_state, dim_z=n_dim_meas, fx=fx, hx=hx, dt=dt, points=points)
    ukf.P = np.eye(n_dim_state) * P
    #ukf.Q = np.eye(n_dim_state) * Q
    ukf.Q = Q_discrete_white_noise(dim=n_dim_state, dt=dt, var=0.004) * Q
    ukf.R = np.eye(n_dim_meas) * R
    ukf.x = np.array([train_data[0], 0])

    # High UKF
    high_ukf = UnscentedKalmanFilter(dim_x=n_dim_state, dim_z=n_dim_meas, fx=high_fx, hx=high_hx, dt=dt, points=points)
    high_ukf.P = np.eye(n_dim_state) * P
    #high_ukf.Q = np.eye(n_dim_state) * Q
    high_ukf.Q = Q_discrete_white_noise(dim=n_dim_state, dt=dt, var=0.004) * Q
    high_ukf.R = np.eye(n_dim_meas) * R
    high_ukf.x = np.array([high_train_data[0], 0])

    # Low UKF
    low_ukf = UnscentedKalmanFilter(dim_x=n_dim_state, dim_z=n_dim_meas, fx=low_fx, hx=low_hx, dt=dt, points=points)
    low_ukf.P = np.eye(n_dim_state) * P
    #low_ukf.Q = np.eye(n_dim_state) * Q
    low_ukf.Q = Q_discrete_white_noise(dim=n_dim_state, dt=dt, var=0.004) * Q
    low_ukf.R = np.eye(n_dim_meas) * R
    low_ukf.x = np.array([low_train_data[0], 0])

    # Xlose
    # Fit the model to the training data
    train_predictions = []
    for z in train_data:
        ukf.predict()
        train_predictions.append(ukf.x[0])
        ukf.update(z)

    # Evaluate the performance on the test set
    test_predictions = []
    for z in test_data:
        ukf.predict()
        test_predictions.append(ukf.x[0])
        ukf.update(z)

    #High
    # Fit the High_model to the training data
    high_train_predictions = []
    for z in high_train_data:
        high_ukf.predict()
        high_train_predictions.append(high_ukf.x[0])
        high_ukf.update(z)

    # Evaluate the performance on the Low_test set
    high_test_predictions = []
    for z in high_test_data:
        high_ukf.predict()
        high_test_predictions.append(high_ukf.x[0])
        high_ukf.update(z)

    # Low
    # Fit the Low_model to the training data
    low_train_predictions = []
    for z in low_train_data:
        low_ukf.predict()
        low_train_predictions.append(low_ukf.x[0])
        low_ukf.update(z)

    # Evaluate the performance on the Low_test set
    low_test_predictions = []
    for z in low_test_data:
        low_ukf.predict()
        low_test_predictions.append(low_ukf.x[0])
        low_ukf.update(z)

    ukf.predict()
    high_ukf.predict()
    low_ukf.predict()

    # Calculate Close  error metrics
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

    # Calculate High Error metrics
    # Calculate error metrics
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

    # Calculate Low Error Metrics
    # Calculate error metrics
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
    return ukf, high_ukf, low_ukf# , metric_df
	
	

eth_symbols = ["ETH/USD"]
btc_symbols = ["BTC/USD"]

btc_ukf, btc_high_ukf, btc_low_ukf = ukf_factory("1H", btc_symbols)
# btc_fift_ukf, btc_fift_high_ukf, btc_fift_low_ukf = ukf_factory("15T", btc_symbols)
# btc_five_ukf, btc_five_high_ukf, btc_five_low_ukf = ukf_factory("5T", btc_symbols)

eth_ukf, eth_high_ukf, eth_low_ukf = ukf_factory("1H", eth_symbols)
# eth_fift_ukf, eth_fift_high_ukf, eth_fift_low_ukf = ukf_factory("15T", eth_symbols)
# eth_five_ukf, eth_five_high_ukf, eth_five_low_ukf = ukf_factory("5T", eth_symbols)

def calculate_metrics(trades, initial_capital, date_index):
    # Calculates backtest metrics for combined trades.

    total_trades = len(trades)
    winning_trades = sum(1 for trade in trades if trade['profit'] > 0)
    losing_trades = sum(1 for trade in trades if trade['profit'] < 0)
    net_profit = sum(trade['profit'] for trade in trades if trade['profit'] > 0)
    net_loss = sum(trade['profit'] for trade in trades if trade['profit'] < 0)
    # total_profit = sum(trade['profit'] for trade in trades)
    total_profit = net_profit - abs(net_loss)
    starting_capital = initial_capital
    ending_capital = initial_capital + total_profit
    returns = ending_capital/initial_capital -1 if initial_capital != 0 else 0
    win_rate = winning_trades / total_trades if total_trades > 0 else 0
    win_pct = win_rate * 100
    average_win = np.mean([trade['profit'] for trade in trades if trade['profit'] > 0]) if winning_trades > 0 else 0
    average_loss = np.mean([trade['profit'] for trade in trades if trade['profit'] < 0]) if losing_trades > 0 else 0
    profit_factor = abs(total_profit / sum(trade['profit'] for trade in trades if trade['profit'] < 0)) if sum(trade['profit'] for trade in trades if trade['profit'] < 0) != 0 else 0
    max_drawdown = calculate_max_drawdown_combined(trades, initial_capital, date_index)
    drawdown_pct = max_drawdown * 100
    total_return_percentage = returns * 100
    duration = len(date_index) / 24

    # Calculate daily returns for combined portfolio
    daily_returns = []
    current_capital = initial_capital
    # Sort trades by entry date to process them chronologically
    sorted_trades = sorted(trades, key=lambda x: x['entry_date'])

    trade_index = 0
    for date in date_index:
        # Add profits from trades that closed on or before this date
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

# Modify calculate_metrics to handle combined trades and initial capital
def calculate_combined_metrics(trades, initial_capital, date_index, final_value):
    # Calculates backtest metrics for combined trades.

    total_trades = len(trades)
    winning_trades = sum(1 for trade in trades if trade['profit'] > 0)
    losing_trades = sum(1 for trade in trades if trade['profit'] < 0)
    net_profit = sum(trade['profit'] for trade in trades if trade['profit'] > 0)
    net_loss = sum(trade['profit'] for trade in trades if trade['profit'] < 0)
    # total_profit = sum(trade['profit'] for trade in trades)
    total_pnl = net_profit - abs(net_loss)
    starting_capital = initial_capital
    # ending_capital = initial_capital + total_profit
    ending_capital = final_value
    total_profit = ending_capital - initial_capital
    commissions = (initial_capital + total_pnl) - final_value
    returns = ending_capital/initial_capital -1 if initial_capital != 0 else 0
    win_rate = winning_trades / total_trades if total_trades > 0 else 0
    win_pct = win_rate * 100
    average_win = np.mean([trade['profit'] for trade in trades if trade['profit'] > 0]) if winning_trades > 0 else 0
    average_loss = np.mean([trade['profit'] for trade in trades if trade['profit'] < 0]) if losing_trades > 0 else 0
    profit_factor = abs(total_profit / sum(trade['profit'] for trade in trades if trade['profit'] < 0)) if sum(trade['profit'] for trade in trades if trade['profit'] < 0) != 0 else 0
    max_drawdown = calculate_max_drawdown_combined(trades, initial_capital, date_index)
    drawdown_pct = max_drawdown * 100
    total_return_percentage = returns * 100
    duration = len(date_index) / 24

    # Calculate daily returns for combined portfolio
    daily_returns = []
    current_capital = initial_capital
    # Sort trades by entry date to process them chronologically
    sorted_trades = sorted(trades, key=lambda x: x['entry_date'])

    trade_index = 0
    for date in date_index:
        # Add profits from trades that closed on or before this date
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

# Modify calculate_max_drawdown to handle combined trades
def calculate_max_drawdown_combined(trades, initial_capital, date_index):
    # Calculates the maximum drawdown for combined trades.

    peak = initial_capital
    max_drawdown = 0
    current_capital = initial_capital

    # Sort trades by entry date
    sorted_trades = sorted(trades, key=lambda x: x['entry_date'])
    trade_index = 0

    for date in date_index:
         # Update current capital based on trades that closed on or before this date
        while trade_index < len(sorted_trades) and sorted_trades[trade_index]['exit_date'] is not None and sorted_trades[trade_index]['exit_date'] <= date:
             current_capital += sorted_trades[trade_index]['profit']
             trade_index += 1

        if current_capital > peak:
            peak = current_capital
        drawdown = (peak - current_capital) / peak if peak != 0 else 0
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown

# Keep the original visualize_trades and calculate_sharpe_ratio as they are used internally
def visualize_trades(data, trades):
    # Visualizes trades on the price chart for a single asset.

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
    plt.title(f"Trades Visualization for {trades[0]['asset'] if trades else 'Asset'}") # Dynamic title
    plt.legend()
    plt.grid(True)
    plt.show()

def calculate_sharpe_ratio(returns, risk_free_rate=0.0025):
    # Calculates the Sharpe ratio.
    if len(returns) < 2:
        return 0
    returns_array = np.array(returns)
    excess_returns = returns_array - risk_free_rate
    sharpe_ratio = np.mean(excess_returns) / np.std(excess_returns) * math.sqrt(365)  # Annualized (365 trading days)
    return sharpe_ratio
	
    
def extractor(input):
    input_str = str(input)
    third = input_str[2]
    fourth = input_str[3]
    goal = third + fourth
    
    return float(goal)
    

# ----------------------------------------------------------------------------------------------------------------	
# start_date = dt.date.today() - dt.timedelta(days = 90)
backtest_duration = 7

length = backtest_duration * 24

# val_bars = crypto_client.get_crypto_bars(request_params)
# btc_val = val_bars.df
btc_val  = pd.read_csv('btc_back30.csv')
# btc_val  = pd.read_csv('btc_back365.csv')
# btc_val  = pd.read_csv('btc_back60.csv')
btc_valid  = pd.read_csv('btc_back366.csv')
# btc_val = btc_valid[:720]
btc_val = btc_valid[-length:]
# btc_val = btc_valid[:length]
print(btc_val)

# val_bars = crypto_client.get_crypto_bars(request_params)
# for last 30 days
eth_val  = pd.read_csv('eth_back30.csv')
# for 365 days
# eth_val  = pd.read_csv('eth_back365.csv')
# For last 60 days
# eth_val  = pd.read_csv('eth_back60.csv')
# for previous month
eth_valid  = pd.read_csv('eth_back366.csv')
# eth_val = eth_valid[:720]
# for last 1 week
eth_val = eth_valid[-length:]
# eth_val = eth_valid[:length]
# eth_val = val_bars.df
print(eth_val)
# ------------------------------------------------------------------------------------------------------------------------------


def backtest_ukf_viz_BTC_ETH_FlexibleAllocation_Aligned2(validation_btc, validation_eth, high_ukf_btc, low_ukf_btc, ukf_btc, high_ukf_eth, low_ukf_eth, ukf_eth, initial_capital=2, visualize=False):
    # Backtests UKF-based trading strategy for BTC and ETH concurrently,
    # assuming dataframes are already aligned by index.

    # Separate trades for each asset
    trades_btc = []
    trades_eth = []

    # Initial total portfolio capital
    total_portfolio_capital = initial_capital
    # REALISM UPGRADE: was a flat 0.25% placeholder applied only on entry,
    # with no exit commission and no funding/slippage modelled at all.
    # Entry commission is now the real Binance maker fee (entries are LIMIT
    # orders). Exit commission, slippage, and funding are computed per-trade
    # inside apply_realistic_exit() / compute_funding_cost() at exit time —
    # see the BTC/ETH exit blocks below.
    commission = MAKER_FEE

    leverage = 10
    
    # Initialize variables for BTC
    positions_btc = 0
    entry_price_btc = 0
    in_trade_btc = False
    exit_trade_btc = False
    tp_price_btc = 0
    sl_price_btc = 0
    leverage_btc = leverage # Leverage is applied to the size of the position

    # Initialize variables for ETH
    positions_eth = 0
    entry_price_eth = 0
    in_trade_eth = False
    exit_trade_eth = False
    tp_price_eth = 0
    sl_price_eth = 0
    leverage_eth = leverage #35 # Leverage is applied to the size of the position

    # Lists to store market prices and predictions for BTC
    market_price_btc = []
    market_high_btc = []
    market_low_btc = []
    pred_close_btc = []
    pred_high_btc = []
    pred_low_btc = []

    # Lists to store market prices and predictions for ETH
    market_price_eth = []
    market_high_eth = []
    market_low_eth = []
    pred_close_eth = []
    pred_high_eth = []
    pred_low_eth = []
    
    part_port = False

    # Add initial data points (assuming validation_btc and validation_eth have at least 2 rows)
    # Check length before accessing index 1
    if len(validation_btc) > 1:
        market_price_btc.append(validation_btc['close'].iloc[1])
        market_high_btc.append(validation_btc['high'].iloc[1])
        market_low_btc.append(validation_btc['low'].iloc[1])
    else:
         print("Warning: BTC validation data has less than 2 rows, cannot initialize market price lists properly.")
         # You might need to handle this edge case depending on your data source

    if len(validation_eth) > 1:
        market_price_eth.append(validation_eth['close'].iloc[1])
        market_high_eth.append(validation_eth['high'].iloc[1])
        market_low_eth.append(validation_eth['low'].iloc[1])
    else:
         print("Warning: ETH validation data has less than 2 rows, cannot initialize market price lists properly.")
         # You might need to handle this edge case depending on your data source


    # --- Removed: Index equality check and pd.merge ---
    # Assuming validation_btc and validation_eth have the same index

    # Iterate through the historical data using the index of one DataFrame
    # Since indices are assumed to be aligned, iterating through validation_btc's index
    # and accessing validation_eth with the same index is safe.
    print("Starting backtest iteration loop (assuming aligned dataframes).")
    for index, row_btc in validation_btc.iterrows():
        # print(validation_eth.head(), validation_btc.info())
        # Directly access the corresponding row in the ETH DataFrame
        indexx = index# + 1 #-------------------------------------------------------------------REMOVE LATER
        row_eth = validation_eth.loc[indexx]

        # --- Add print statement to confirm loop is running ---
        # print(f"Processing timestamp: {index}")
        # --- End print statement ---

        ## Process BTC ##
        # Update market price lists
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

        # UKF Prediction and Update for BTC
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

        # BTC Entry Signal Generation
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
        rr_btc = 8.6 #5.05 # 3.8 4.65 7.6
        strict_btc = 0.0025 #25 #0.0015 0.004 *0.0025 0.00275 0.0035
        
        stricts_btc = 0.005 #4 # 0.006 0.007 0.00525 0.0045
# ----------------------------------------------------------------------------------------------btc_params

        if pct_diff_btc <= stricts_btc or vol_btc >= stricts_btc: # 0.00075 0.00125 0.000525
            #print("Criteria for BTC SIggen under current tuning reached [PRIMARY], hold for Secondary Checks(strict)")
            if trend_btc == "up":
                buy_price_candidate_btc = low_pred_btc
                tp_price_candidate_btc = pred_btc
                prof_btc = abs(1 - (tp_price_candidate_btc / buy_price_candidate_btc))
                #print("btc", pct_diff_btc, prof_btc)

                if tp_price_candidate_btc > buy_price_candidate_btc and prof_btc >= strict_btc:
                    # print(f"{row_btc['timestamp']}, entry: {buy_price_candidate_btc}.")
                    if buy_price_candidate_btc >= row_btc['low'] and buy_price_candidate_btc <= row_btc['high']:
                        signal_btc = "BUY"
                        entry_price_candidate_btc = buy_price_candidate_btc
                        
                        tp_candidate_btc = entry_price_candidate_btc * btc_buy_tp
                        # print(f"Pct_diff = {pct_diff_btc}, vol = {vol_btc}")
                        if tp_price_candidate_btc > tp_candidate_btc:
                            tp_price_candidate_btc = tp_candidate_btc
                        else:
                            tp_price_candidate_btc = tp_price_candidate_btc
                        # tp_price_candidate_btc = tp_price_candidate_btc

            elif trend_btc == "down":
                sell_price_candidate_btc = high_pred_btc
                tp_price_candidate_btc = pred_btc
                prof_btc = abs(1 - (sell_price_candidate_btc / tp_price_candidate_btc))
                #print("btc", pct_diff_btc, prof_btc)

                if sell_price_candidate_btc > tp_price_candidate_btc and prof_btc >= strict_btc:
                    # print(f"{row_btc['timestamp']}, entry: {sell_price_candidate_btc}.")
                    if sell_price_candidate_btc >= row_btc['low'] and sell_price_candidate_btc <= row_btc['high']:
                        signal_btc = "SELL"
                        entry_price_candidate_btc = sell_price_candidate_btc
                        
                        tp_candidate_btc = entry_price_candidate_btc * btc_buy_tp
                        # print(f"Pct_diff = {pct_diff_btc}, vol = {vol_btc}")
                        if tp_price_candidate_btc > tp_candidate_btc:
                            tp_price_candidate_btc = tp_candidate_btc
                        else:
                            tp_price_candidate_btc = tp_price_candidate_btc

        ## Process ETH ##
        # Update market price lists
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

        # UKF Prediction and Update for ETH
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

        # ETH Entry Signal Generation
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

        # qty = (bal * lev) / entry_price
        # hold_val = price * qty                        ###(price * lev) * qty
        # req_inc = hold_val + 15%(bal)
        # tp = req_inc / qty
        
        # qty = (bal * lev) / entry
        # tp = ((price * qty) + bal * 1.15) / qty
        # avail_eqty = total_portfolio_capital
        # tp_qty = (avail_eqty * leverage) / entry
        
        eth_buy_tp = 1.0085
        eth_sell_tp = 2 - eth_buy_tp
        rr_eth = 9.6 # 4.95 # 3.8 # 1.38
        strict_eth = 0.0035 #55 # 0.00725 0.0035 0.00475 0.00525 0.0045 6
        stricts_eth = 0.006 #8 # 0.00325 0.00225 0.006 *0.0075 0.0085 0.005 6
#--------------------------------------------------------------------------------------------------------eth_params

        if pct_diff_eth <= stricts_eth or vol_eth >= stricts_eth: # 0.00525 0.00125
            # print("Criteria for ETH SIggen under current tuning reached [PRIMARY], hold for Secondary Checks(strict)")
            if trend_eth == "up":
                buy_price_candidate_eth = low_pred_eth
                tp_price_candidate_eth = pred_eth
                prof_eth = abs(1 - (tp_price_candidate_eth / buy_price_candidate_eth))
                #print("eth", pct_diff_eth, prof_eth)

                if tp_price_candidate_eth > buy_price_candidate_eth and prof_eth >= strict_eth:
                    # print(f"{row_eth['timestamp']}, entry: {buy_price_candidate_eth}.")
                    if buy_price_candidate_eth >= row_eth['low'] and buy_price_candidate_eth <= row_eth['high']:
                        signal_eth = "BUY"
                        entry_price_candidate_eth = buy_price_candidate_eth
                        
                        tp_candidate_eth = entry_price_candidate_eth * eth_buy_tp
                        # print(f"Pct_diff = {pct_diff_eth}, vol = {vol_eth}")
                        if tp_price_candidate_eth > tp_candidate_eth:
                            tp_price_candidate_eth = tp_candidate_eth
                        else:
                            tp_price_candidate_eth = tp_price_candidate_eth

            elif trend_eth == "down":
                sell_price_candidate_eth = high_pred_eth
                tp_price_candidate_eth = pred_eth
                prof_eth = abs(1 - (sell_price_candidate_eth / tp_price_candidate_eth))
                #print("eth", pct_diff_eth, prof_eth)

                if sell_price_candidate_eth > tp_price_candidate_eth and prof_eth >= strict_eth:
                    # print(f"{row_eth['timestamp']}, entry: {sell_price_candidate_eth}.")
                    if sell_price_candidate_eth >= row_eth['low'] and sell_price_candidate_eth <= row_eth['high']:
                        signal_eth = "SELL"
                        entry_price_candidate_eth = sell_price_candidate_eth
                        
                        tp_candidate_eth = entry_price_candidate_eth * eth_sell_tp
                        # print(f"Pct_diff = {pct_diff_eth}, vol = {vol_eth}")
                        if tp_price_candidate_eth < tp_candidate_eth:
                            tp_price_candidate_eth = tp_candidate_eth
                        else:
                            tp_price_candidate_eth = tp_price_candidate_eth

        
        ## Portfolio Allocation and Trade Execution ##
        
        btc_buy_sl = 0.005
        btc_sell_sl = 1 - btc_buy_sl
        eth_buy_sl = 0.005
        eth_sell_sl = 1 - eth_buy_sl
        Max_Loss = total_portfolio_capital * 16
        btc_Max_Loss = (total_portfolio_capital * 8.5) * 32.5 # 15 7.5
        stop_lossed = False
        # Now, handle new entries based on available signals and whether we are already in a trade
        if signal_btc and signal_eth and not in_trade_btc and not in_trade_eth:
            # leverage_commission = total_portfolio_capital * leverage_btc * commission
            # total_portfolio_capital = total_portfolio_capital - leverage_commission
            # Signal from both, split capital
            capital_btc = total_portfolio_capital / 2
            capital_eth = total_portfolio_capital / 2
            # Max_Loss = total_portfolio_capital * 1.6
            total_portfolio_capital = 0 # All capital allocated to new trades

            # Execute BTC Trade
            # REALISM UPGRADE: entry_price_candidate_btc is the THEORETICAL
            # trigger price the signal aimed for. The actual fill now goes
            # through apply_realistic_entry(), which applies LIMIT-order
            # slippage, rounds to the real exchange lot step, and rejects
            # the trade if it can't clear Binance's minimum notional floor —
            # exactly what would happen live.
            trigger_price_btc = entry_price_candidate_btc
            tp_price_btc = tp_price_candidate_btc
            entry_price_btc, positions_btc, commission_fees, _btc_valid = \
                apply_realistic_entry(trigger_price_btc, signal_btc,
                                       capital_btc, leverage_btc, BTC_LOT_STEP)

            if not _btc_valid:
                print(f"{row_btc['timestamp']}: BTC signal REJECTED — "
                      f"position size after rounding fails minimum notional "
                      f"(${MIN_NOTIONAL}). Capital returned, no trade placed.")
                total_portfolio_capital += capital_btc
                in_trade_btc = False
            else:
                # ADJUST SO IT CHECKS IF STOP LOSS LEADS TO LIQUIDATION, SET TO ONLY RISK 40% of portfolio
                Maximum_Loss = btc_Max_Loss / (entry_price_btc * positions_btc)
                # sl_price_btc = entry_price_btc * btc_buy_sl if signal_btc == 'BUY' else entry_price_btc * btc_sell_sl
                sl_price_btc = entry_price_btc - Maximum_Loss if signal_btc == 'BUY' else entry_price_btc + Maximum_Loss
                # sl_price_btc = entry_price_btc - (1/rr_btc * (tp_price_btc - entry_price_btc)) if signal_btc == 'BUY' else entry_price_btc + (1/rr_btc * (entry_price_btc - tp_price_btc))
                # sl_price_btc = ((positions_btc * entry_price_btc) * stake) / positions_btc  if signal_btc == 'BUY' else ((positions_btc * entry_price_btc) * (2 - stake)) / positions_btc # Stop loss by drawdown(2.5%)# Stop loss by drawdown(2.5%)
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
                    'sl_price': sl_price_btc
                })
                # print(f"Timestamp {index}: Both signals. Entered BTC {signal_btc} at {entry_price_btc:.2f} with capital {capital_btc:.2f}")
                print(f"{row_btc['timestamp']}: Both signals. Entered BTC {signal_btc} at {entry_price_btc:.2f} "
                      f"(trigger was {trigger_price_btc:.2f}), tp @ {tp_price_btc:.2f}, sl @ {sl_price_btc:.2f} "
                      f"with capital {capital_btc:.2f}")


            # Execute ETH Trade
            trigger_price_eth = entry_price_candidate_eth
            tp_price_eth = tp_price_candidate_eth
            entry_price_eth, positions_eth, commission_fees, _eth_valid = \
                apply_realistic_entry(trigger_price_eth, signal_eth,
                                       capital_eth, leverage_eth, ETH_LOT_STEP)

            if not _eth_valid:
                print(f"{row_eth['timestamp']}: ETH signal REJECTED — "
                      f"position size after rounding fails minimum notional "
                      f"(${MIN_NOTIONAL}). Capital returned, no trade placed.")
                total_portfolio_capital += capital_eth
                in_trade_eth = False
            else:
                # ADJUST SO IT CHECKS IF STOP LOSS LEADS TO LIQUIDATION, SET TO ONLY RISK 40% of portfolio
                Maximum_Loss = Max_Loss / (entry_price_eth * positions_eth)
                # sl_price_eth = entry_price_eth * eth_buy_sl if signal_eth == 'BUY' else entry_price_eth * eth_sell_sl
                sl_price_eth = entry_price_eth - Maximum_Loss if signal_eth == 'BUY' else entry_price_eth + Maximum_Loss
                # sl_price_eth = entry_price_eth - (1/rr_eth * (tp_price_eth - entry_price_eth)) if signal_eth == 'BUY' else entry_price_eth + (1/rr_eth * (entry_price_eth - tp_price_eth))
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
                    'sl_price': sl_price_eth
                })
                # print(f"Timestamp {index}: Both signals. Entered ETH {signal_eth} at {entry_price_eth:.2f} with capital {capital_eth:.2f}")
                print(f"{row_eth['timestamp']}: Both signals. Entered ETH {signal_eth} at {entry_price_eth:.2f} "
                      f"(trigger was {trigger_price_eth:.2f}), tp @ {tp_price_eth:.2f}, sl @ {sl_price_eth:.2f} "
                      f"with capital {capital_eth:.2f}")
            
            part_port = True


        # trying to be able to open a new position if 2 positions awere open and 1 has closed so there's available margin
        elif signal_btc and not signal_eth and not in_trade_btc and not in_trade_eth:
             # Signal only from BTC, go all-in
            # leverage_commission = total_portfolio_capital * leverage_btc * commission
            # total_portfolio_capital = total_portfolio_capital - leverage_commission
            capital_btc = total_portfolio_capital
            # Max_Loss = total_portfolio_capital * 1.6
            total_portfolio_capital = 0 # All capital allocated to BTC

            # Execute BTC Trade
            trigger_price_btc = entry_price_candidate_btc
            tp_price_btc = tp_price_candidate_btc
            entry_price_btc, positions_btc, commission_fees, _btc_valid = \
                apply_realistic_entry(trigger_price_btc, signal_btc,
                                       capital_btc, leverage_btc, BTC_LOT_STEP)

            if not _btc_valid:
                print(f"{row_btc['timestamp']}: BTC signal REJECTED — "
                      f"position size after rounding fails minimum notional "
                      f"(${MIN_NOTIONAL}). Capital returned, no trade placed.")
                total_portfolio_capital += capital_btc
                in_trade_btc = False
            else:
                # ADJUST SO IT CHECKS IF STOP LOSS LEADS TO LIQUIDATION, SET TO ONLY RISK 40% of portfolio
                Maximum_Loss = btc_Max_Loss / (entry_price_btc * positions_btc)
                # sl_price_btc = entry_price_btc * btc_buy_sl if signal_btc == 'BUY' else entry_price_btc * btc_sell_sl
                sl_price_btc = entry_price_btc - Maximum_Loss if signal_btc == 'BUY' else entry_price_btc + Maximum_Loss
                # sl_price_btc = entry_price_btc - (1/rr_btc * (tp_price_btc - entry_price_btc)) if signal_btc == 'BUY' else entry_price_btc + (1/rr_btc * (entry_price_btc - tp_price_btc))
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
                    'sl_price': sl_price_btc
                })
                # print(f"Timestamp {index}: BTC signal only. Entered BTC {signal_btc} at {entry_price_btc:.2f} with capital {capital_btc:.2f}")
                print(f"{row_btc['timestamp']}: BTC signal only. Entered BTC {signal_btc} at {entry_price_btc:.2f} "
                      f"(trigger was {trigger_price_btc:.2f}), tp @ {tp_price_btc:.2f}, sl @ {sl_price_btc:.2f} "
                      f"with capital {capital_btc:.2f}")


        elif not signal_btc and signal_eth and not in_trade_btc and not in_trade_eth:
            # Signal only from ETH, go all-in
            # leverage_commission = total_portfolio_capital * leverage_eth * commission
            # total_portfolio_capital = total_portfolio_capital - leverage_commission
            capital_eth = total_portfolio_capital
            # Max_Loss = total_portfolio_capital * 1.6
            total_portfolio_capital = 0 # All capital allocated to ETH

            # Execute ETH Trade
            trigger_price_eth = entry_price_candidate_eth
            tp_price_eth = tp_price_candidate_eth
            entry_price_eth, positions_eth, commission_fees, _eth_valid = \
                apply_realistic_entry(trigger_price_eth, signal_eth,
                                       capital_eth, leverage_eth, ETH_LOT_STEP)

            if not _eth_valid:
                print(f"{row_eth['timestamp']}: ETH signal REJECTED — "
                      f"position size after rounding fails minimum notional "
                      f"(${MIN_NOTIONAL}). Capital returned, no trade placed.")
                total_portfolio_capital += capital_eth
                in_trade_eth = False
            else:
                # ADJUST SO IT CHECKS IF STOP LOSS LEADS TO LIQUIDATION, SET TO ONLY RISK 40% of portfolio
                Maximum_Loss = Max_Loss / (entry_price_eth * positions_eth)
                # sl_price_eth = entry_price_eth * eth_buy_sl if signal_eth == 'BUY' else entry_price_eth * eth_sell_sl
                sl_price_eth = entry_price_eth - Maximum_Loss if signal_eth == 'BUY' else entry_price_eth + Maximum_Loss
                # sl_price_eth = entry_price_eth - (1/rr_eth * (tp_price_eth - entry_price_eth)) if signal_eth == 'BUY' else entry_price_eth + (1/rr_eth * (entry_price_eth - tp_price_eth))
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
                    'sl_price': sl_price_eth
                })
                # print(f"Timestamp {index}: ETH signal only. Entered ETH {signal_eth} at {entry_price_eth:.2f} with capital {capital_eth:.2f}")
                print(f"{row_eth['timestamp']}: ETH signal only. Entered ETH {signal_eth} at {entry_price_eth:.2f} "
                      f"(trigger was {trigger_price_eth:.2f}), tp @ {tp_price_eth:.2f}, sl @ {sl_price_eth:.2f} "
                      f"with capital {capital_eth:.2f}")
            # Note: If signals are generated while already in a trade for one or both assets,
        # new trades are not entered based on this logic. This is a design choice.
        

        # First, handle exits for any Open trades
        # BTC Exit Logic
        if in_trade_btc:
            latest_trade_btc = trades_btc[-1]
            exit_trade_btc = False
            exit_price_btc = None
            # REALISM UPGRADE: exit_leg_btc tracks WHICH exit fired (SL/TP/
            # MARKET) so the correct slippage model can be applied below.
            # This is purely bookkeeping — none of the trigger conditions or
            # their evaluation order are changed.
            exit_leg_btc = None
            entry_price_btc = latest_trade_btc['entry_price']
            net_profit = leverage_btc * ((row_btc['high'] - latest_trade_btc['entry_price']) * positions_btc) if latest_trade_btc['signal'] == 'BUY' else leverage_btc * ((latest_trade_btc['entry_price'] - row_btc['low']) * positions_btc)
            net_drawdown = leverage_btc * ((row_btc['low'] - latest_trade_btc['entry_price']) * positions_btc) if latest_trade_btc['signal'] == 'BUY' else leverage_btc * ((latest_trade_btc['entry_price'] - row_btc['high']) * positions_btc)
            drawdown_price = row_btc['high'] if latest_trade_btc['signal'] == 'SELL' else row_btc['low']
            net_equity = ((positions_btc * entry_price_btc) + net_profit)
            print(f"{row_btc['timestamp']}: Net Profit: {net_profit} \n Net Drawdown: {net_drawdown}, Drawdown Price: {drawdown_price}")
            
            # EXIT FOR BTC BUY SIGNAL
            # NOTE — PESSIMISTIC WORST-CASE ORDERING, INTENTIONALLY UNCHANGED:
            # The Stop Loss check below is evaluated FIRST, before Take Profit.
            # On any candle where both the SL and TP levels fall within the
            # same bar's high/low range, OHLCV data cannot tell us which was
            # actually touched first intra-candle — so this backtest always
            # assumes the worst case (SL hit first). This systematically
            # understates performance versus what's achievable live, which is
            # the deliberate, desired bias. Do not reorder these conditions.
            if latest_trade_btc['signal'] == 'BUY':
                if row_btc['low'] <= latest_trade_btc['sl_price']: # Stop Loss
                    exit_trade_btc = True
                    exit_price_btc = latest_trade_btc['sl_price']
                    exit_leg_btc = 'SL'
                    if abs(net_drawdown) >= net_equity:
                        print("--------------------------------------------------Account_liquidated----------------------------------------------------------")
                elif net_profit >= (capital_btc * 0.225):
                    print("BTC Net Profit reached 7.5% of margin, exiting trade")
                    exit_trade_btc = True
                    # exit_price_btc = price_btc # Exit at current market price
                    if row_btc['high'] >= latest_trade_btc['tp_price']:
                        exit_price_btc = latest_trade_btc['tp_price']
                        exit_leg_btc = 'TP'
                    else:
                        exit_price_btc = price_btc # Exit at current market price on reversal
                        exit_leg_btc = 'MARKET'
                elif row_btc['high'] >= latest_trade_btc['tp_price']: # Take Profit
                    exit_trade_btc = True
                    exit_price_btc = latest_trade_btc['tp_price']
                    exit_leg_btc = 'TP'
                
                if not exit_trade_btc:
                    if trend_btc == "down" and pct_diff_btc >= 0.00005: #Signal Reversal
                        # Check if reversal signal is strong enough
                        exit_trade_btc = True
                        exit_price_btc = price_btc # Exit at current market price on reversal
                        exit_leg_btc = 'MARKET'
                        print("Signal for reveral Triggered")
                    
            # EXIT FOR BTC SELL SIGNAL
            elif latest_trade_btc['signal'] == 'SELL':
                if row_btc['high'] >= latest_trade_btc['sl_price']: # Stop Loss
                    exit_trade_btc = True
                    exit_price_btc = latest_trade_btc['sl_price']
                    exit_leg_btc = 'SL'
                    # elif net_profit < 0 and net_equity < abs(net_profit):
                    if abs(net_drawdown) >= net_equity:
                        print("---------------------------------------------------Account_liquidated---------------------------------------------------------")
                elif net_profit >= (capital_btc * 0.225):
                    print("BTC Net Profit reached 7.5% of margin, exiting trade")
                    exit_trade_btc = True
                    # exit_price_btc = price_btc # Exit at current market price
                    if row_btc['low'] <= latest_trade_btc['tp_price']:
                        exit_price_btc = latest_trade_btc['tp_price']
                        exit_leg_btc = 'TP'
                    else:
                        exit_price_btc = price_btc # Exit at current market price
                        exit_leg_btc = 'MARKET'
                        
                elif row_btc['low'] <= latest_trade_btc['tp_price']: # Take Profit
                    exit_trade_btc = True
                    exit_price_btc = latest_trade_btc['tp_price']
                    exit_leg_btc = 'TP'
                if not exit_trade_btc:
                    if trend_btc == "down" and pct_diff_btc >= 0.00005: #Signal Reversal
                         # Check if reversal signal is strong enough
                        exit_trade_btc = True
                        exit_price_btc = price_btc # Exit at current market price on reversal
                        exit_leg_btc = 'MARKET'
                        print("Signal for reveral Triggered")
            
            if exit_trade_btc:
                # REALISM UPGRADE: SL/TP fills now go through apply_realistic_exit()
                # for slippage + taker commission. MARKET exits (reversal /
                # profit-threshold-without-TP-touch) already use the actual
                # candle price, so they only pick up the taker commission —
                # there's no better reference price to slip against.
                if exit_leg_btc in ('SL', 'TP'):
                    exit_price_btc, exit_commission_fee = apply_realistic_exit(
                        exit_price_btc, exit_leg_btc, latest_trade_btc['signal'], positions_btc
                    )
                else:
                    exit_commission_fee = TAKER_FEE * positions_btc

                funding_cost = compute_funding_cost(
                    latest_trade_btc.get('entry_timestamp'), row_btc['timestamp'],
                    positions_btc, entry_price_btc, leverage_btc
                )

                profit_btc = leverage_btc * ((exit_price_btc - latest_trade_btc['entry_price']) * positions_btc) if latest_trade_btc['signal'] == 'BUY' else leverage_btc * ((latest_trade_btc['entry_price'] - exit_price_btc) * positions_btc)
                profit_btc -= exit_commission_fee
                profit_btc -= funding_cost
                # total_portfolio_capital += (positions_btc * exit_price_btc) # Add value back to total capital
                total_portfolio_capital += ((positions_btc * entry_price_btc) + profit_btc)
                positions_btc = 0
                in_trade_btc = False
                latest_trade_btc['exit_date'] = row_btc.name
                latest_trade_btc['exit_price'] = exit_price_btc
                latest_trade_btc['exit_leg'] = exit_leg_btc
                latest_trade_btc['funding_cost'] = funding_cost
                latest_trade_btc['exit_commission'] = exit_commission_fee
                latest_trade_btc['profit'] = profit_btc
                profit_or_loss = None
                if latest_trade_btc['profit'] > 0:
                    profit_or_loss = "Profit"
                else:
                    profit_or_loss = "Loss"
                print(f"{row_btc['timestamp']}: Exited BTC {latest_trade_btc['signal']} trade ({exit_leg_btc}) with "
                      f"{profit_or_loss} {profit_btc:.2f} (funding: -{funding_cost:.4f}, exit fee: -{exit_commission_fee:.4f}), "
                      f"capital {total_portfolio_capital:.2f}")


        # ETH Exit Logic
        if in_trade_eth:
            latest_trade_eth = trades_eth[-1]
            exit_trade_eth = False
            exit_price_eth = None
            # REALISM UPGRADE: leg tracking only, see BTC block comment above
            # for full explanation — SL-first worst-case ordering untouched.
            exit_leg_eth = None
            entry_price_eth = latest_trade_eth['entry_price']
            net_profit = leverage_eth * ((row_eth['high'] - latest_trade_eth['entry_price']) * positions_eth) if latest_trade_eth['signal'] == 'BUY' else leverage_eth * ((latest_trade_eth['entry_price'] - row_eth['low']) * positions_eth)
            drawdown_price = row_eth['high'] if latest_trade_eth['signal'] == 'SELL' else row_eth['low']
            net_drawdown = leverage_eth * ((row_eth['low'] - latest_trade_eth['entry_price']) * positions_eth) if latest_trade_eth['signal'] == 'BUY' else leverage_eth * ((latest_trade_eth['entry_price'] - row_eth['high']) * positions_eth)
            net_equity = ((positions_eth * entry_price_eth) + net_profit)
            print(f"{row_eth['timestamp']}: Net Profit: {net_profit} \n Net Drawdown: {net_drawdown}, DrawDown Price: {drawdown_price}")
            
            # FOR ETH BUY SIGNAL
            # NOTE — PESSIMISTIC WORST-CASE ORDERING, INTENTIONALLY UNCHANGED.
            # See the comment above the BTC exit block for the full rationale.
            if latest_trade_eth['signal'] == 'BUY':
                
                if row_eth['low'] <= latest_trade_eth['sl_price']: # Stop Loss
                    exit_trade_eth = True
                    exit_price_eth = latest_trade_eth['sl_price']
                    exit_leg_eth = 'SL'
                    # elif net_profit < 0 and net_equity < abs(net_profit):
                    if abs(net_drawdown) >= net_equity:
                        print("---------------------------------------------------Account_liquidated---------------------------------------------------------")
                # TO EXIT A TRADE IF THE PNL REACHES THRESHOLD
                elif net_profit >= (capital_eth * 0.175):
                    print("Net Profit reached 5% of margin, exiting trade")
                    exit_trade_eth = True
                    if row_eth['high'] >= latest_trade_eth['tp_price']:
                        exit_price_eth = latest_trade_eth['tp_price']
                        exit_leg_eth = 'TP'
                    else:
                        exit_price_eth = price_eth # Exit at current market price
                        exit_leg_eth = 'MARKET'
                        # SHOULD WORK BETTER IN THE LIVE ENVIRONMENT, NOW USING THE TP_PRICE LOGIC SHOULD HELP BE A BIT REALISTIC

                elif row_eth['high'] >= latest_trade_eth['tp_price']: # Take Profit
                    exit_trade_eth = True
                    exit_price_eth = latest_trade_eth['tp_price']
                    exit_leg_eth = 'TP'
                
                if not exit_trade_eth:
                    if trend_eth == "down" and pct_diff_eth >= 0.00005: #Signal Reversal
                         # Check if reversal signal is strong enough
                        exit_trade_eth = True
                        exit_price_eth = price_eth # Exit at current market price on reversal
                        exit_leg_eth = 'MARKET'
                        print("Signal for reveral Triggered")
            
            # FOR ETH SELL SIGNAL
            elif latest_trade_eth['signal'] == 'SELL':
                
                if row_eth['high'] >= latest_trade_eth['sl_price']: # Stop Loss
                    exit_trade_eth = True
                    exit_price_eth = latest_trade_eth['sl_price']
                    exit_leg_eth = 'SL'
                    # elif net_profit < 0 and net_equity < abs(net_profit):
                    if abs(net_drawdown) >= net_equity:
                        print("-----------------------------------------------Account_liquidated-------------------------------------------------------------")
                elif net_profit >= (capital_eth * 0.175):
                    print("Net Profit reached 5% of margin, exiting trade")
                    exit_trade_eth = True
                    if row_eth['low'] <= latest_trade_eth['tp_price']:
                        exit_price_eth = latest_trade_eth['tp_price']
                        exit_leg_eth = 'TP'
                    else:
                        exit_price_eth = price_eth # Exit at current market price
                        exit_leg_eth = 'MARKET'

                elif row_eth['low'] <= latest_trade_eth['tp_price']: # Take Profit
                    exit_trade_eth = True
                    exit_price_eth = latest_trade_eth['tp_price']
                    exit_leg_eth = 'TP'
                
                if not exit_trade_eth:
                    if trend_eth == "down" and pct_diff_eth >= 0.00005: #Signal Reversal
                        # Check if reversal signal is strong enough
                        exit_trade_eth = True
                        exit_price_eth = price_eth # Exit at current market price on reversal
                        exit_leg_eth = 'MARKET'
                        print("Signal for reveral Triggered")

            if exit_trade_eth:
                # REALISM UPGRADE: same treatment as the BTC exit block —
                # SL/TP fills slip and pay taker commission, MARKET exits
                # only pay taker commission.
                if exit_leg_eth in ('SL', 'TP'):
                    exit_price_eth, exit_commission_fee = apply_realistic_exit(
                        exit_price_eth, exit_leg_eth, latest_trade_eth['signal'], positions_eth
                    )
                else:
                    exit_commission_fee = TAKER_FEE * positions_eth

                funding_cost = compute_funding_cost(
                    latest_trade_eth.get('entry_timestamp'), row_eth['timestamp'],
                    positions_eth, entry_price_eth, leverage_eth
                )

                profit_eth = leverage_eth * ((exit_price_eth - latest_trade_eth['entry_price']) * positions_eth) if latest_trade_eth['signal'] == 'BUY' else leverage_eth * ((latest_trade_eth['entry_price'] - exit_price_eth) * positions_eth)
                profit_eth -= exit_commission_fee
                profit_eth -= funding_cost
                # total_portfolio_capital += (positions_eth * exit_price_eth) # Add value back to total capital
                total_portfolio_capital += ((positions_eth * entry_price_eth) + profit_eth)
                positions_eth = 0
                in_trade_eth = False
                latest_trade_eth['exit_date'] = row_eth.name
                latest_trade_eth['exit_price'] = exit_price_eth
                latest_trade_eth['exit_leg'] = exit_leg_eth
                latest_trade_eth['funding_cost'] = funding_cost
                latest_trade_eth['exit_commission'] = exit_commission_fee
                latest_trade_eth['profit'] = profit_eth
                profit_or_loss = None
                if latest_trade_eth['profit'] > 0:
                    profit_or_loss = "Profit"
                else:
                    profit_or_loss = "Loss"
                print(f"{row_eth['timestamp']}: Exited ETH {latest_trade_eth['signal']} trade ({exit_leg_eth}) with "
                      f"{profit_or_loss} {profit_eth:.2f} (funding: -{funding_cost:.4f}, exit fee: -{exit_commission_fee:.4f}), "
                      f"capital {total_portfolio_capital:.2f}")

    # Combine trades from both assets for overall metrics
    all_trades = trades_btc + trades_eth

    # Calculate final portfolio value (remaining cash + value of Open positions + profits from closed trades)
    final_portfolio_value = total_portfolio_capital
    if in_trade_btc:
        # Estimate value of Open BTC position using the last known price
        final_portfolio_value += positions_btc * market_price_btc[-1] if market_price_btc else 0
    if in_trade_eth:
         # Estimate value of Open ETH position using the last known price
        final_portfolio_value += positions_eth * market_price_eth[-1] if market_price_eth else 0


    # Calculate metrics based on combined trades and initial capital
    btc_results = calculate_metrics(trades_btc, initial_capital, validation_btc.index)
    eth_results = calculate_metrics(trades_eth, initial_capital, validation_eth.index)
    results = calculate_combined_metrics(all_trades, initial_capital, validation_btc.index, final_portfolio_value)
    
    print(f"BTC results, \n {btc_results} \n ETH results, \n {eth_results}")

    if visualize:
        # Visualize trades for each asset separately
        if trades_btc:
            print("Visualizing BTC Trades:")
            visualize_trades(validation_btc, trades_btc)
        if trades_eth:
            print("\nVisualizing ETH Trades:")
            visualize_trades(validation_eth, trades_eth)

    return results
	

# print(btc_val, eth_val)
results = backtest_ukf_viz_BTC_ETH_FlexibleAllocation_Aligned2(btc_val, eth_val, btc_high_ukf, btc_low_ukf, btc_ukf, eth_high_ukf, eth_low_ukf, eth_ukf, initial_capital=5, visualize=False)
print(f"Total Backtest Metrics results, \n {results}")