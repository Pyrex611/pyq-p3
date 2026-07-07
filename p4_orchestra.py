"""
pyquant_orchestra.py
Master scheduler for pyq_p4. Runs:
  - daily_optimizer at 00:03 (data refresh + Bayesian parameter search)
  - hourly_task,  fifteen_minute_task,  five_minute_task,  one_minute_task

CHANGES FROM the pyq_p3 orchestrator:
  - Imports from pyq_p4 (major refactor: ATR-adaptive SL, independent
    BTC/ETH trading, dynamic last-resort stop, 10x leverage).
  - grid_search.run_grid_search() now runs Bayesian optimization via Optuna
    instead of a blind grid — no changes needed here since the function
    name/signature/return shape are unchanged for interface compatibility.
  - hourly_task is now scheduled at :02 past the hour instead of :00. Alpaca's
    crypto bar ingestion typically lags 30-90 seconds behind the wall clock;
    running exactly on the hour risked reading a still-forming or stale bar.
    This was identified and recommended earlier but never actually applied
    to the schedule — fixed here as part of this refactor pass.
"""

import json
import time
import traceback
import schedule

from pyq_p4 import hourly_task, fifteen_minute_task, five_minute_task, one_minute_task
from data_download import data_download
from p4_search import run_grid_search


def daily_optimizer():
    print("[Orchestra] Starting daily optimisation…")
    try:
        # 1. Download fresh historical data (also refreshes the CSVs grid_search.py reads)
        btc_val, eth_val = data_download()

        # 2. Run the Bayesian parameter search against the freshly downloaded data.
        #    Returns strict_btc/stricts_btc/strict_eth/stricts_eth/atr_mult/best_returns.
        best_params = run_grid_search()

        # 3. Persist optimal params so hourly_task can read atr_mult and the
        #    signal thresholds on its next run.
        with open("optimal_params.json", "w") as f:
            json.dump(best_params, f, indent=4)

        print(f"[Orchestra] Optimisation complete. Best params saved: {best_params}")
    except Exception as e:
        print(f"[Orchestra] daily_optimizer error: {e}")
        print(traceback.format_exc())


# ── Schedule setup ────────────────────────────────────────────────────────────

schedule.every().day.at("00:03").do(daily_optimizer)

# CHANGED: :00 -> :02 to allow Alpaca's crypto bar ingestion lag to settle
# before hourly_task reads the "latest" 1H bar.
schedule.every().hour.at(":02").do(hourly_task)

for _offset in [":00", ":15", ":30", ":45"]:
    schedule.every().hour.at(_offset).do(fifteen_minute_task)

for _offset in [":00", ":05", ":10", ":15", ":20", ":25",
                ":30", ":35", ":40", ":45", ":50", ":55"]:
    schedule.every().hour.at(_offset).do(five_minute_task)

schedule.every(1).minutes.do(one_minute_task)

# ── Main loop ─────────────────────────────────────────────────────────────────
print("[Orchestra] Scheduler running (pyq_p4). Press Ctrl-C to stop.")
try:
    while True:
        schedule.run_pending()
        time.sleep(1)
except KeyboardInterrupt:
    print("[Orchestra] Shutdown requested.")
except Exception as e:
    print(f"[Orchestra] Fatal error: {e}")
    print(traceback.format_exc())
