"""
data_download.py
Downloads historical BTC and ETH OHLCV data from Alpaca and saves to CSV.
Exposes data_download() as a callable for the orchestrator and grid search.
"""

import os
import datetime as dt
import pandas as pd
from dotenv import load_dotenv

from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.historical import CryptoHistoricalDataClient

load_dotenv()

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")


def data_download(lookback_days: int = 366) -> tuple:
    """
    Downloads the last `lookback_days` of hourly BTC/USD and ETH/USD bars
    from Alpaca, saves them as CSVs, and returns the DataFrames.

    Returns:
        tuple: (btc_val, eth_val) as pandas DataFrames
    """
    crypto_client = CryptoHistoricalDataClient(
        api_key=ALPACA_API_KEY,
        secret_key=ALPACA_SECRET_KEY
    )

    start_date = dt.date.today() - dt.timedelta(days=lookback_days)

    # --- BTC ---
    btc_request = CryptoBarsRequest(
        symbol_or_symbols=["BTC/USD"],
        timeframe=TimeFrame.Hour,
        start=start_date
    )
    btc_bars = crypto_client.get_crypto_bars(btc_request)
    btc_val = btc_bars.df.xs("BTC/USD", level="symbol")
    btc_val = btc_val.reset_index()

    # --- ETH ---
    eth_request = CryptoBarsRequest(
        symbol_or_symbols=["ETH/USD"],
        timeframe=TimeFrame.Hour,
        start=start_date
    )
    eth_bars = crypto_client.get_crypto_bars(eth_request)
    eth_val = eth_bars.df.xs("ETH/USD", level="symbol")
    eth_val = eth_val.reset_index()

    # Save primary CSVs used by grid_search and backtest
    btc_val.to_csv("btc_back366.csv", index=False)
    eth_val.to_csv("eth_back366.csv", index=False)

    # Also save as btc_back365.csv (used by ukf_factory for training)
    btc_val.to_csv("btc_back365.csv", index=False)
    eth_val.to_csv("eth_back365.csv", index=False)

    print(f"[data_download] Downloaded {len(btc_val)} BTC rows and {len(eth_val)} ETH rows.")
    print("[data_download] Saved: btc_back366.csv, eth_back366.csv, btc_back365.csv, eth_back365.csv")

    return btc_val, eth_val


# Allow running directly as a script
if __name__ == "__main__":
    btc, eth = data_download()
    print(btc.tail())
    print(eth.tail())
