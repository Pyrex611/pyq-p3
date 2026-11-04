"""
p4_orchestra.py
Master scheduler for pyq_p4. Run directly (`python3 p4_orchestra.py`) on a
continuously-running host (the VPS path — see VPS_SETUP.md), it schedules:
  - daily_optimizer at 00:03 (data refresh + Bayesian parameter search + UKF re-anchoring)
  - hourly_task at :02 past the hour
  - fifteen_minute_task at :00, :15, :30, :45
  - five_minute_task at 5-minute intervals
  - one_minute_task every minute

As of the GitHub Actions cron migration, this file's scheduling/loop code
is guarded behind `if __name__ == "__main__":` so `daily_optimizer()` can
also be imported directly by `cron_runner.py` (the stateless, one-shot
entrypoint used by `.github/workflows/orchestra.yml`) without starting a
second infinite loop. See `CLAUDE.md` Section 6.3 and `cron_runner.py`'s
own docstring for the full picture of why a continuous process (this
file) and a stateless cron tick (`cron_runner.py`) now both exist.
"""

import json
import time
import traceback
from datetime import datetime, timezone
import schedule

import pyq_p4
from pyq_p4 import hourly_task, fifteen_minute_task, five_minute_task, one_minute_task
from data_download import data_download
from p4_search import run_grid_search
import pyquant_state as state_store


def daily_optimizer():
    # Cron migration: daily_optimizer() previously had no run-guard of its
    # own — it relied entirely on schedule.every().day.at("00:03") firing
    # exactly once within one continuous process. A cron-triggered process
    # (GitHub Actions' */5 * * * * schedule, see cron_runner.py) calling
    # this unconditionally on every matching tick needs this guard so it
    # still only actually runs once per day.
    #
    # 2026-10-27 — found via a live "no new signal for 3 days" report:
    # the guard used to wrap BOTH the UKF re-anchor (fast: two Alpaca bar
    # fetches + a UKF fit) AND the Bayesian/grid parameter search (slow:
    # up to 150 Optuna trials over a year of hourly data for two
    # symbols), as one all-or-nothing try/except, with the UKF re-anchor
    # called LAST. `orchestra.yml`'s job has a 10-minute timeout; if the
    # search alone ran past it, GitHub Actions kills the whole process —
    # not a Python exception this code ever sees — AFTER the day's claim
    # was already taken (try_claim_run() claims up front, before any of
    # the slow work) and BEFORE retrain_daily_ukfs() ever ran. Every tick
    # for the rest of that day then hit the "already ran… skipping" guard
    # above, and the live 1H UKFs went un-re-anchored — exactly the kind
    # of slow drift that stops the strategy from firing signals, with
    # nothing in any log or the UI pointing at why (GitHub Actions' own
    # "job was cancelled" note is easy to miss and wasn't being recorded
    # anywhere this platform reads).
    #
    # Fix: two independent guards/try-excepts instead of one, and the
    # cheap, must-happen UKF re-anchor goes FIRST so a parameter-search
    # timeout can no longer starve it — matching the one thing that
    # actually has to happen daily for live signal generation to track
    # walkforward.py's own simulated retrain cadence. A heartbeat is also
    # written either way so GET /api/heartbeat (Live Monitor page) can
    # show real pass/fail status instead of requiring a GitHub Actions
    # log dig every time this is in doubt.
    _run_daily_ukf_retrain()
    _run_daily_param_search()
    _run_shadow_param_search()


def _run_daily_ukf_retrain():
    if not state_store.try_claim_run("ukf_retrain", state_store.current_day_bucket()):
        print("[Orchestra] ukf_retrain already ran (or is running) today. Skipping.")
        return
    print("[Orchestra] Re-anchoring 1H UKFs…")
    try:
        pyq_p4.retrain_daily_ukfs()
        state_store.set_json("pyquant_ukf_retrain_heartbeat", {
            "ts": datetime.now(timezone.utc).isoformat(), "status": "ok",
        })
        print("[Orchestra] UKF re-anchor complete.")
    except Exception as e:
        err = f"{e}\n{traceback.format_exc()}"
        print(f"[Orchestra] ukf_retrain error: {err}")
        try:
            state_store.set_json("pyquant_ukf_retrain_heartbeat", {
                "ts": datetime.now(timezone.utc).isoformat(), "status": "failed", "error": str(e),
            })
        except Exception as e2:
            print(f"[Orchestra] ALSO failed to record the ukf_retrain failure itself: {e2}")


def _run_daily_param_search():
    if not state_store.try_claim_run("daily_optimizer", state_store.current_day_bucket()):
        print("[Orchestra] daily_optimizer already ran (or is running) today. Skipping.")
        return

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

    except Exception as e:
        err = f"{e}\n{traceback.format_exc()}"
        print(f"[Orchestra] daily_optimizer error: {err}")
        # 2026-10-27: record this as a real, visible failed run (not just
        # a print easy to miss in GitHub Actions' log) — same pattern as
        # the UI-submitted-backtest flow (request_backtest + failed
        # update_run_status). include_failed=true on GET /api/runs shows
        # it; a param-search failure no longer silently repeats for 24h
        # with nothing to point at why.
        try:
            rid = state_store.request_backtest(run_type="daily_optimizer", params={}, symbol="BTC+ETH")
            if rid is not None:
                state_store.update_run_status(rid, "failed", error=str(e))
        except Exception as e2:
            print(f"[Orchestra] ALSO failed to record the daily_optimizer failure itself: {e2}")


def _run_shadow_param_search():
    """2026-11-03 (shadow signal comparison) — runs walkforward.py's OWN
    optimizer (run_standalone_grid_search()) standalone, on the same
    daily cadence as the real optimizer above, and stores its output
    separately (pyquant_kv key "walkforward_optimal_params") so
    hourly_task() can record a side-by-side SHADOW diagnostic every
    tick without ever touching what actually trades — see pyq_p4.py's
    SignalGenerator.load_params(), the one place that decides which
    source ("p4_search" or "walkforward", the `live_signal_source` kv
    setting) actually drives real orders.

    Off by default (`shadow_comparison_enabled` kv flag, flip via the
    Settings page) and placed LAST, after both the UKF retrain and the
    real param search, deliberately — see _run_daily_param_search()'s
    2026-10-27 incident note just above: orchestra.yml's job has a
    10-minute timeout, and a Bayesian search here can itself take
    several minutes (up to BAYESIAN_TRIALS Optuna trials). Running a
    SECOND full search back-to-back with the first risks exactly the
    kind of timeout that incident describes — so this only runs when
    explicitly enabled, and a timeout here can never prevent the UKF
    retrain or the real optimizer from completing, since both already
    ran above by the time this is reached."""
    if not state_store.get_json("shadow_comparison_enabled", False):
        return
    if not state_store.try_claim_run("shadow_optimizer", state_store.current_day_bucket()):
        print("[Orchestra] shadow_optimizer already ran (or is running) today. Skipping.")
        return

    print("[Orchestra] Starting shadow (walkforward) optimisation…")
    try:
        import walkforward  # imported here, not at module top, so a
        # failure to import walkforward.py's heavier dependency set
        # (matplotlib/filterpy/optuna — all already in requirements.txt,
        # but kept lazy in case that ever changes) can never block
        # daily_optimizer()'s two calls above from running.
        prev_params = state_store.get_json("walkforward_optimal_params")
        best_params = walkforward.run_standalone_grid_search(prev_params=prev_params)
        state_store.set_json("walkforward_optimal_params", best_params)
        print(f"[Orchestra] Shadow optimisation complete. Params: {best_params}")
        try:
            state_store.record_run(
                run_type="shadow_optimizer",
                metrics={},
                params=best_params,
            )
        except Exception as e:
            print(f"[Orchestra] shadow run logging failed (non-fatal): {e}")
    except Exception as e:
        err = f"{e}\n{traceback.format_exc()}"
        print(f"[Orchestra] shadow_optimizer error: {err}")
        try:
            rid = state_store.request_backtest(run_type="shadow_optimizer", params={}, symbol="BTC+ETH")
            if rid is not None:
                state_store.update_run_status(rid, "failed", error=str(e))
        except Exception as e2:
            print(f"[Orchestra] ALSO failed to record the shadow_optimizer failure itself: {e2}")


# Guarded behind __main__ (added during the GitHub Actions cron migration)
# so this module can be safely imported — e.g. by cron_runner.py, which
# needs daily_optimizer without wanting this file's own infinite loop to
# start — while `python3 p4_orchestra.py` from the command line (the VPS
# deployment path in VPS_SETUP.md) behaves exactly as before.
if __name__ == "__main__":
    # ── Schedule setup ────────────────────────────────────────────────────
    schedule.every().day.at("00:03").do(daily_optimizer)

    # Scheduled at :02 past the hour to allow Alpaca bar ingestion lag to settle
    schedule.every().hour.at(":02").do(hourly_task)

    for _offset in [":00", ":15", ":30", ":45"]:
        schedule.every().hour.at(_offset).do(fifteen_minute_task)

    for _offset in [":00", ":05", ":10", ":15", ":20", ":25",
                    ":30", ":35", ":40", ":45", ":50", ":55"]:
        schedule.every().hour.at(_offset).do(five_minute_task)

    schedule.every(1).minutes.do(one_minute_task)

    # ── Main loop ─────────────────────────────────────────────────────────
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