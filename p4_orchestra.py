"""
p4_orchestra.py
Master scheduler for pyq_p4. Run directly (`python3 p4_orchestra.py`) on a
continuously-running host (the VPS path — see VPS_SETUP.md), it schedules:
  - daily_optimizer at 00:03 — internally: UKF re-anchor fires immediately;
    the real (p4_search) param search waits until >=01:00 UTC; the
    optional shadow (walkforward) param search waits until >=01:30 UTC —
    staggered (2026-11-04) so the two heavy searches never share one
    process/job (10-minute GitHub Actions timeout risk — see
    _run_daily_param_search()'s docstring). A pending manual "Run
    Optimizer Now" request (Settings page) pre-empts this schedule and
    runs on the very next tick instead, at most one optimizer per tick.
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
    # 2026-11-12 (manual "Anchor Now" trigger, Settings page) — cheap
    # (two Alpaca fetches + a UKF fit), so unlike the two heavy
    # optimizers below it never needs to preempt anything else this
    # tick; just run it unconditionally before the normal schedule.
    _run_pending_manual_anchor()
    _run_daily_ukf_retrain()
    # 2026-11-04 (manual trigger + staggered scheduling): a pending
    # "Run Optimizer Now" request from the Settings page (see
    # _run_pending_manual_optimizer()'s docstring) takes priority over
    # the normal clock-gated schedule below, and runs AT MOST ONE heavy
    # optimizer this tick even if both were requested together — never
    # both back-to-back in the same process/job. When nothing manual is
    # pending, fall through to the normal automatic schedule, which is
    # now staggered across separate ticks by clock time rather than
    # relying on "first finishes, then second starts" within one call:
    # _run_daily_param_search() only proceeds at/after 01:00 UTC,
    # _run_shadow_param_search() at/after 01:30 UTC — each gets its own
    # cron invocation's full 10-minute GitHub Actions budget instead of
    # sharing one.
    if not _run_pending_manual_optimizer():
        _run_daily_param_search()
        _run_shadow_param_search()


def _past_utc_clock(hour: int, minute: int = 0, now: datetime = None) -> bool:
    """2026-11-04: the staggering gate for the two heavy optimizers
    below — `now` defaults to the real current time; only ever passed
    explicitly by a test. Checked BEFORE try_claim_run() so an early
    tick doesn't consume the day's claim before the clock is actually
    right; the function just returns and the next tick (5 min later)
    checks again."""
    now = now or datetime.now(timezone.utc)
    return (now.hour, now.minute) >= (hour, minute)


def _run_pending_manual_optimizer() -> bool:
    """2026-11-04 (manual "Run Optimizer Now" trigger) — checks the
    `manual_optimizer_request` kv flag (set by POST /api/optimizer/
    trigger, Settings page) and, if anything is pending, runs AT MOST
    ONE of the two heavy optimizers this tick — p4_search checked
    first. Returns True if it ran something (the caller then skips the
    normal automatic schedule entirely for this tick).

    "At most one" is deliberate: a "run both now" click sets both
    flags at once, and running them back-to-back in the same process
    would reintroduce exactly the 10-minute GitHub Actions timeout risk
    the 01:00/01:30 UTC staggering below exists to avoid. The second
    flag, if any, simply stays pending and fires on the NEXT cron tick
    (~5 minutes later) instead — still both "now" by any human
    standard, just not the same process.

    A manual run bypasses BOTH the clock gate and the day-bucket guard
    (it claims its own unique bucket, via `manual=True` below) and, for
    the shadow optimizer, the `shadow_comparison_enabled` toggle too —
    wanting one data point right now is a reasonable thing to ask for
    even without committing to the standing nightly cadence. It does
    NOT, however, prevent the normal automatic run from also happening
    later that day at its own scheduled time — a manual run is treated
    as an extra one-off, not a substitute for the day's claim."""
    pending = state_store.get_json("manual_optimizer_request") or {}
    if pending.get("p4_search"):
        _clear_manual_optimizer_flag("p4_search")
        print("[Orchestra] Manual trigger: running p4_search optimizer now…")
        _run_daily_param_search(manual=True)
        return True
    if pending.get("walkforward"):
        _clear_manual_optimizer_flag("walkforward")
        print("[Orchestra] Manual trigger: running walkforward (shadow) optimizer now…")
        _run_shadow_param_search(manual=True)
        return True
    return False


def _clear_manual_optimizer_flag(which: str) -> None:
    """Clears `which`'s flag BEFORE running (not after) so a crash
    mid-search can never leave this tick-locked into retrying the same
    expensive search every 5 minutes forever — if it fails, the owner
    sees the failed run (same recorded-failure pattern as the automatic
    path) and can just click the button again."""
    try:
        pending = state_store.get_json("manual_optimizer_request") or {}
        pending[which] = False
        state_store.set_json("manual_optimizer_request", pending)
    except Exception as e:
        print(f"[Orchestra] Failed to clear manual_optimizer_request[{which}] (non-fatal): {e}")


def _run_daily_ukf_retrain(manual: bool = False):
    """`manual=True` (only via _run_pending_manual_anchor(), itself only
    reachable via the Settings page's "Anchor Now" button) bypasses the
    day-bucket guard via a one-off bucket, same "extra one-off, not a
    substitute for the day's own claim" pattern the two heavy optimizers
    already use below — a manual anchor today does not stop the normal
    automatic retrain from also claiming and running later the same day."""
    bucket = (f"manual-{state_store.current_minute_bucket()}" if manual
              else state_store.current_day_bucket())
    if not state_store.try_claim_run("ukf_retrain", bucket):
        print("[Orchestra] ukf_retrain already ran (or is running) today. Skipping.")
        return
    print(f"[Orchestra] Re-anchoring 1H UKFs{' (manual trigger)' if manual else ''}…")
    try:
        pyq_p4.retrain_daily_ukfs()
        state_store.set_json("pyquant_ukf_retrain_heartbeat", {
            "ts": datetime.now(timezone.utc).isoformat(), "status": "ok",
            **({"triggered_by": "manual"} if manual else {}),
        })
        print("[Orchestra] UKF re-anchor complete.")
        # 2026-11-09: this is the one-a-day "still alive" Telegram ping
        # (replaces the old every-cron-tick "scheduler ONLINE" alert —
        # see p4_utils.py's run_scheduler() comment). Piggybacks on this
        # try_claim_run("ukf_retrain", current_day_bucket()) guard so it
        # can never fire more than once per day, same as the retrain
        # itself. Best-effort: a failed alert must never fail the
        # retrain it's reporting on. A manual anchor (2026-11-12) gets
        # its own distinct wording since it's an extra, one-off run, not
        # the day's "still alive" signal.
        try:
            pyq_p4.guard.add_alert_to_queue(
                "⚓ PyQuant UKF anchor complete (manual trigger)." if manual else
                "🔔 PyQuant daily UKF anchor complete — engine active.")
        except Exception as alert_err:
            print(f"[Orchestra] Daily active-ping alert failed (non-fatal): {alert_err}")
    except Exception as e:
        err = f"{e}\n{traceback.format_exc()}"
        print(f"[Orchestra] ukf_retrain error: {err}")
        try:
            state_store.set_json("pyquant_ukf_retrain_heartbeat", {
                "ts": datetime.now(timezone.utc).isoformat(), "status": "failed", "error": str(e),
                **({"triggered_by": "manual"} if manual else {}),
            })
        except Exception as e2:
            print(f"[Orchestra] ALSO failed to record the ukf_retrain failure itself: {e2}")


def _run_pending_manual_anchor() -> bool:
    """2026-11-12 (manual "Anchor Now" trigger, Settings page) — checks
    the `manual_anchor_request` kv flag (set by POST /api/anchor/
    trigger) and, if set, re-anchors the 1H UKFs right now via
    _run_daily_ukf_retrain(manual=True) (its own one-off bucket, so it
    never consumes or is blocked by the day's normal automatic claim —
    same pattern as _run_pending_manual_optimizer() below). Re-anchoring
    is cheap (two Alpaca bar fetches + a UKF fit per symbol), so unlike
    the two heavy optimizers this never needs to preempt anything else
    in the same tick; daily_optimizer() just calls this unconditionally.
    Clears the flag before running, same reasoning as
    _clear_manual_optimizer_flag(): a crash mid-retrain should never
    tick-lock a retry loop — the owner sees the failure (heartbeat
    status="failed") and can just click the button again."""
    if not state_store.get_json("manual_anchor_request", False):
        return False
    try:
        state_store.set_json("manual_anchor_request", False)
    except Exception as e:
        print(f"[Orchestra] Failed to clear manual_anchor_request (non-fatal): {e}")
    print("[Orchestra] Manual trigger: re-anchoring UKFs now…")
    _run_daily_ukf_retrain(manual=True)
    return True


def _run_daily_param_search(manual: bool = False):
    """2026-11-04: staggered to fire at/after 01:00 UTC on the normal
    automatic schedule (was "as soon as a tick lands after midnight") —
    see _run_pending_manual_optimizer()'s docstring for why: so this and
    _run_shadow_param_search() below never share a process/job even
    when both are due the same day. `manual=True` (set only by
    _run_pending_manual_optimizer(), itself only reachable via the
    Settings page's "Run Optimizer Now" button) bypasses both the clock
    gate and the day-bucket guard via a one-off bucket, as an EXTRA run
    alongside (not instead of) the day's normal claim."""
    if manual:
        bucket = f"manual-{state_store.current_minute_bucket()}"
    else:
        if not _past_utc_clock(1, 0):
            return
        bucket = state_store.current_day_bucket()
    if not state_store.try_claim_run("daily_optimizer", bucket):
        print("[Orchestra] daily_optimizer already ran (or is running) today. Skipping.")
        return

    print(f"[Orchestra] Starting daily optimisation{' (manual trigger)' if manual else ''}…")
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
                metrics={"btc_val": btc_val, "eth_val": eth_val, "triggered_by": "manual" if manual else "schedule"},
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


def _run_shadow_param_search(manual: bool = False):
    """2026-11-03 (shadow signal comparison) — runs walkforward.py's OWN
    optimizer (run_standalone_grid_search()) standalone, and stores its
    output separately (pyquant_kv key "walkforward_optimal_params") so
    hourly_task() can record a side-by-side SHADOW diagnostic every
    tick without ever touching what actually trades — see pyq_p4.py's
    SignalGenerator.load_params(), the one place that decides which
    source ("p4_search" or "walkforward", the `live_signal_source` kv
    setting) actually drives real orders.

    Off by default on the automatic schedule (`shadow_comparison_enabled`
    kv flag, flip via the Settings page) and, when on, staggered to fire
    at/after 01:30 UTC — 30 minutes after _run_daily_param_search()'s own
    01:00 UTC gate (2026-11-04; previously both ran back-to-back in the
    same daily_optimizer() call) — see that function's 2026-10-27
    incident note: orchestra.yml's job has a 10-minute timeout, and a
    Bayesian search here can itself take several minutes. Giving each
    optimizer its OWN cron tick, rather than sharing one, means a
    timeout in either can never prevent the other from completing.

    `manual=True` (see _run_pending_manual_optimizer()) bypasses the
    clock gate, the `shadow_comparison_enabled` toggle, and the day-
    bucket guard (via a one-off bucket) — wanting one data point right
    now is reasonable even without committing to the nightly cadence."""
    if manual:
        bucket = f"manual-{state_store.current_minute_bucket()}"
    else:
        if not state_store.get_json("shadow_comparison_enabled", False):
            return
        if not _past_utc_clock(1, 30):
            return
        bucket = state_store.current_day_bucket()
    if not state_store.try_claim_run("shadow_optimizer", bucket):
        print("[Orchestra] shadow_optimizer already ran (or is running) today. Skipping.")
        return

    print(f"[Orchestra] Starting shadow (walkforward) optimisation{' (manual trigger)' if manual else ''}…")
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
                metrics={"triggered_by": "manual" if manual else "schedule"},
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