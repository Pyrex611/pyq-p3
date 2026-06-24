"""
pyquant_orchestra.py
Master scheduler.  Runs:
  - daily_optimizer at 00:03 (data refresh + grid search)
  - hourly_task,  fifteen_minute_task,  five_minute_task,  one_minute_task
"""

import json
import time
import traceback
import schedule

# FIX: orchestra was importing from pyq_p2; the current module is pyq_p3
from pyq_p3 import hourly_task, fifteen_minute_task, five_minute_task, one_minute_task
from data_download import data_download
from grid_search import run_grid_search


def daily_optimizer():
    print("[Orchestra] Starting daily optimisation…")
    try:
        # 1. Download fresh historical data (also refreshes the CSVs for grid search)
        btc_val, eth_val = data_download()

        # 2. Run grid search against the freshly downloaded data
        best_params = run_grid_search()

        # 3. Persist optimal params so other modules can read them if needed
        with open("optimal_params.json", "w") as f:
            json.dump(best_params, f, indent=4)

        print(f"[Orchestra] Optimisation complete. Best params saved: {best_params}")
    except Exception as e:
        print(f"[Orchestra] daily_optimizer error: {e}")
        print(traceback.format_exc())


# ── Schedule setup ────────────────────────────────────────────────────────────

schedule.every().day.at("00:03").do(daily_optimizer)

schedule.every().hour.at(":00").do(hourly_task)

for _offset in [":00", ":15", ":30", ":45"]:
    schedule.every().hour.at(_offset).do(fifteen_minute_task)

for _offset in [":00", ":05", ":10", ":15", ":20", ":25",
                ":30", ":35", ":40", ":45", ":50", ":55"]:
    schedule.every().hour.at(_offset).do(five_minute_task)

schedule.every(1).minutes.do(one_minute_task)

# ── Main loop ─────────────────────────────────────────────────────────────────
print("[Orchestra] Scheduler running. Press Ctrl-C to stop.")
try:
    while True:
        schedule.run_pending()
        time.sleep(1)
except KeyboardInterrupt:
    print("[Orchestra] Shutdown requested.")
except Exception as e:
    print(f"[Orchestra] Fatal error: {e}")
    print(traceback.format_exc())
