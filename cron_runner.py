"""
cron_runner.py — stateless, one-shot entrypoint for the GitHub Actions
cron migration (see CLAUDE.md Section 6.3 and GITHUB_ACTIONS_SETUP.md).

p4_orchestra.py (the VPS path) is one continuous process that stays alive
forever, ticking its scheduled tasks from inside its own infinite loop.
This file is the opposite shape on purpose: it runs ONCE, calls every
task that might be due, and exits — meant to be invoked fresh by a
GitHub Actions workflow on a `*/5 * * * *` schedule (5 minutes is
GitHub's documented minimum cron granularity; there is no 1-minute
option, which is why `one_minute_task()` effectively runs on a 5-minute
cadence under this architecture — see GITHUB_ACTIONS_SETUP.md for the
full trade-off writeup).

Every task function called below already self-guards against running
more often than it should (see pyquant_state.py's try_claim_run() and
the bucket helpers it uses) — hourly_task/fifteen_minute_task/
one_minute_task/daily_optimizer all no-op if their own time bucket has
already been claimed. That means this script can safely call ALL of
them, every single invocation, in order from most- to least-frequent,
and each one decides for itself whether it's actually due:

    one_minute_task()      — claims a 1-minute bucket (in practice, the
                              first 5-minute-cron invocation to reach a
                              given minute)
    five_minute_task()     — no bucket guard; matches this script's own
                              cadence 1:1, so it simply runs every time
    fifteen_minute_task()  — claims a 15-minute bucket
    hourly_task()           — claims an hour bucket
    daily_optimizer()      — claims a day bucket

Importing pyq_p4 (below) is what actually creates/restores the 18 UKF
models and the tracker/flag globals — see that module's own comments
right after its UKF-creation block for why this matters here
specifically (a cron-restarted process has no continuous memory between
runs, so this restore step is what makes that safe).
"""
from __future__ import annotations

import sys
import traceback

import pyq_p4
from pyq_p4 import hourly_task, fifteen_minute_task, five_minute_task, one_minute_task
import p4_orchestra  # safe to import now — its scheduler/loop is behind __main__


def run_one_cron_tick():
    tasks = [
        ("one_minute_task", one_minute_task),
        ("five_minute_task", five_minute_task),
        ("fifteen_minute_task", fifteen_minute_task),
        ("hourly_task", hourly_task),
        ("daily_optimizer", p4_orchestra.daily_optimizer),
    ]
    failures = []
    for name, fn in tasks:
        try:
            print(f"[cron_runner] Calling {name}()…")
            fn()
        except Exception as e:
            # Every task above already wraps its own body in try/except
            # internally (matching this project's fail-open logging
            # pattern) — reaching this except means something escaped
            # that, which is worth a non-zero exit so the GitHub Actions
            # run shows as failed rather than silently green.
            print(f"[cron_runner] {name}() raised: {e}\n{traceback.format_exc()}")
            failures.append(name)

    if failures:
        print(f"[cron_runner] Completed with failures in: {failures}")
        sys.exit(1)
    print("[cron_runner] Tick complete, no uncaught errors.")


if __name__ == "__main__":
    run_one_cron_tick()
