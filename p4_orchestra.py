"""
p4_orchestra.py
Master scheduler for pyq_p4. Runs:
  - daily_optimizer at 00:03 (data refresh + Bayesian parameter search + UKF re-anchoring)
  - hourly_task at :02 past the hour
  - fifteen_minute_task at :00, :15, :30, :45
  - five_minute_task at 5-minute intervals
  - one_minute_task every minute
"""

import json
import time
import traceback
import schedule

import pyq_p4
from pyq_p4 import hourly_task, fifteen_minute_task, five_minute_task, one_minute_task
from data_download import data_download
from p4_search import run_grid_search
import pyquant_state as state_store


def daily_optimizer():
    print("[Orchestra] Starting daily optimisation…")
    try:
        # 1. Download fresh historical data
        btc_val, eth_val = data_download()

        # 2. Run the Bayesian parameter search against the freshly downloaded data
        best_params = run_grid_search()

        # 3. Persist optimal params for live execution.
        # Phase 1: the state store (SQLite/Postgres) is now the primary
        # write — SignalGenerator.load_params() reads it first. The local
        # file is kept as a best-effort human-readable mirror only.
        state_store.set_json("optimal_params", best_params)
        try:
            with open("optimal_params.json", "w") as f:
                json.dump(best_params, f, indent=4)
        except Exception as e:
            print(f"[Orchestra] optimal_params.json mirror write failed (non-fatal): {e}")

        print(f"[Orchestra] Optimisation complete. Best params saved: {best_params}")

        # Phase 2: log this optimizer run for the UI's "Backtest" screen
        # (run history), alongside walkforward.py's own logged runs.
        try:
            state_store.record_run(
                run_type="daily_optimizer",
                metrics={"btc_val": btc_val, "eth_val": eth_val},
                params=best_params,
            )
        except Exception as e:
            print(f"[Orchestra] run logging failed (non-fatal): {e}")

        # 4. Re-anchor live 1H UKF models on the new trailing 30-day window (matches walkforward.py)
        pyq_p4.retrain_daily_ukfs()

    except Exception as e:
        print(f"[Orchestra] daily_optimizer error: {e}")
        print(traceback.format_exc())


# ── Schedule setup ────────────────────────────────────────────────────────────

schedule.every().day.at("00:03").do(daily_optimizer)

# Scheduled at :02 past the hour to allow Alpaca bar ingestion lag to settle
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