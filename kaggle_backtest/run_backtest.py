"""
kaggle_backtest/run_backtest.py — runs on Kaggle (a script kernel), either
automatically (triggered by api/index.py's POST /api/backtests/sync-to-
kaggle pushing a new version, which Kaggle auto-runs) OR manually, by
opening this exact kernel in Kaggle's own editor and clicking "Save & Run
All" yourself. See KAGGLE.md Part K for the full manual-trigger workflow
this was rewritten (2026-10-26) to support properly.

Why this exists as a *manual* option at all, alongside the GitHub Actions
path (.github/workflows/backtest.yml): every fully-automated Kaggle-kernel
run has, so far, either failed opaquely (Kaggle's UI showing "Failed"
with no log at all — see PLAN.md's 2026-10-25/26 entries) or succeeded
silently with nothing to debug when it didn't. A human clicking "Save &
Run All" in Kaggle's own editor gets Kaggle's live, visible console
output directly — a completely different, more reliable execution
context than an API-triggered background run. The owner asked for this
path specifically so a stuck/opaque automated run can always be re-run
by hand, with full visibility, and with the parameters right there to
tweak before doing so.

### What changed 2026-10-26 (read this before editing the RUN PARAMETERS
### block below)

Previously, overrides came from a separate `params.json` file pushed
alongside this script — invisible unless you specifically went looking
for it in Kaggle's file browser, and not something you could sensibly
hand-edit before a manual re-run. They are now plain Python constants
in THIS file (the "RUN PARAMETERS" block right below), because that's
the one file Kaggle's editor always shows you and lets you edit directly.
`api/index.py`'s push function rewrites these same lines every time it
pushes a new version (so "Run Backtest"/"Sync to Kaggle" from the UI
still works exactly as before) — editing them here and clicking "Save &
Run All" yourself is an equally valid, independent way to start a run
with different values, no UI round-trip needed.

RUN_ID handling is deliberately forgiving of both triggers: if RUN_ID
points at a row that's still "queued" (the normal case right after an
automated push), this run updates that exact row. Otherwise — a manual
re-run with no fresh push, or RUN_ID left as None — it creates a brand
new pyquant_runs row itself, tagged `metrics["source"] = "kaggle_manual"`
so the UI/Compare page can tell the two triggers apart if that's ever
useful. Either way the result lands in the same shared Supabase table
the rest of the platform already reads from.
"""
import os
import subprocess
import sys
import traceback

# ── RUN PARAMETERS ───────────────────────────────────────────────────────
# Edit any of these directly in Kaggle's editor, then click "Save & Run
# All" to re-run with different values — no PyQuant UI round-trip needed.
# `None` means "use walkforward.py's own module-level default" for that
# field. api/index.py's push function rewrites this whole block verbatim
# on every automated push; hand edits survive until the next push.
RUN_ID = None                 # int (an existing queued run's id) or None
WALK_FORWARD_DAYS = None      # int, e.g. 15
BAYESIAN_TRIALS = None        # int, e.g. 150
LEVERAGE = None               # float, e.g. 10.0
USE_BAYESIAN = None           # True / False / None
USE_ATR_SL = None             # True / False / None
DATASET_SLUG = "REPLACE_ME/pyquant-historical-data"  # owner/slug, see KAGGLE.md Part E
# ── END RUN PARAMETERS ───────────────────────────────────────────────────

REPO_DIR = "/kaggle/working/pyq-p3"


def _run(cmd: list) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def main() -> None:
    print("[run_backtest] Starting.", flush=True)

    if not os.path.isdir(REPO_DIR):
        _run(["git", "clone", "--depth", "1",
              "https://github.com/Pyrex611/pyq-p3.git", REPO_DIR])
    sys.path.insert(0, REPO_DIR)
    print("[run_backtest] Repo ready.", flush=True)

    _run([sys.executable, "-m", "pip", "install", "-q", "-r",
          os.path.join(REPO_DIR, "requirements.txt")])
    print("[run_backtest] Dependencies installed.", flush=True)

    # DATABASE_URL must be set before pyquant_state.py's first call — it's
    # read lazily (os.environ.get) inside that module, not at import time,
    # so this is safe to do right before the first state_store call below.
    from kaggle_secrets import UserSecretsClient
    os.environ["DATABASE_URL"] = UserSecretsClient().get_secret("DATABASE_URL")
    print("[run_backtest] DATABASE_URL secret loaded.", flush=True)

    sys.path.insert(0, REPO_DIR)
    import pyquant_state as state_store

    params = {
        "walk_forward_days": WALK_FORWARD_DAYS,
        "bayesian_trials": BAYESIAN_TRIALS,
        "leverage": LEVERAGE,
        "use_bayesian": USE_BAYESIAN,
        "use_atr_sl": USE_ATR_SL,
        "dataset_slug": DATASET_SLUG,
    }

    # Robust run_id handling (2026-10-26) — see module docstring. Reuse the
    # queued row from an automated push if it's still "queued"; otherwise
    # (a manual re-run, or RUN_ID left as None) create a fresh one so this
    # execution always has somewhere real to report into.
    run_id = RUN_ID
    is_manual_trigger = True
    if run_id is not None:
        existing = state_store.get_run(run_id)
        if existing is not None and existing.get("status") == "queued":
            is_manual_trigger = False
        else:
            print(f"[run_backtest] RUN_ID={run_id} isn't a fresh 'queued' row "
                  f"(status={existing.get('status') if existing else 'not found'}) — "
                  f"creating a new run instead.", flush=True)
            run_id = None
    if run_id is None:
        run_id = state_store.request_backtest(run_type="walkforward", params=params, symbol="BTC+ETH")
        if run_id is None:
            raise RuntimeError("request_backtest() returned None — state store write failed; "
                                "check DATABASE_URL is reachable from this Kaggle kernel.")
    print(f"[run_backtest] run_id={run_id} (manual_trigger={is_manual_trigger})", flush=True)

    try:
        state_store.update_run_status(run_id, "running")
        print("[run_backtest] Marked run 'running'.", flush=True)

        # Historical data: copy this kernel's attached dataset into the
        # cloned repo, matching the filenames walkforward.py's module-level
        # BTC_CSV/ETH_CSV constants expect.
        dataset_dir = f"/kaggle/input/{DATASET_SLUG.split('/')[-1]}"
        import shutil
        shutil.copy(os.path.join(dataset_dir, "btc_back366.csv"), os.path.join(REPO_DIR, "btc_back366.csv"))
        shutil.copy(os.path.join(dataset_dir, "eth_back366.csv"), os.path.join(REPO_DIR, "eth_back366.csv"))
        print("[run_backtest] Historical-data CSVs copied in.", flush=True)

        os.chdir(REPO_DIR)
        import walkforward

        # Module-constant overrides — see CLAUDE.md Section 6.1 for why
        # this approach (not a signature refactor of run_walk_forward())
        # was chosen: it's zero-risk to the validated backtest engine's
        # internals, which read these same names throughout.
        if WALK_FORWARD_DAYS is not None:
            walkforward.WALK_FORWARD_DAYS = int(WALK_FORWARD_DAYS)
        if BAYESIAN_TRIALS is not None:
            walkforward.BAYESIAN_TRIALS = int(BAYESIAN_TRIALS)
        if USE_BAYESIAN is not None:
            walkforward.USE_BAYESIAN = bool(USE_BAYESIAN)
        if LEVERAGE is not None:
            walkforward.LEVERAGE = float(LEVERAGE)
        if USE_ATR_SL is not None:
            walkforward.USE_ATR_SL = bool(USE_ATR_SL)

        walkforward._SUPPRESS_AUTO_RECORD_RUN = True

        print(
            f"[run_backtest] Params: WALK_FORWARD_DAYS={walkforward.WALK_FORWARD_DAYS} "
            f"BAYESIAN_TRIALS={walkforward.BAYESIAN_TRIALS} LEVERAGE={walkforward.LEVERAGE} "
            f"USE_BAYESIAN={walkforward.USE_BAYESIAN} USE_ATR_SL={walkforward.USE_ATR_SL}",
            flush=True,
        )

        print("[run_backtest] Calling walkforward.run_walk_forward()…", flush=True)
        metrics, all_trades, capital_curve, params_log = walkforward.run_walk_forward()
        metrics_dict = dict(zip(metrics["Metric"], metrics["Value"])) if not metrics.empty else {}
        metrics_dict["trade_count"] = len(all_trades)
        metrics_dict["source"] = "kaggle_manual" if is_manual_trigger else "kaggle_auto"

        state_store.update_run_status(run_id, "completed", metrics=metrics_dict)
        print(f"[run_backtest] run {run_id} completed: {metrics_dict}", flush=True)

    except Exception as e:
        err = f"{e}\n{traceback.format_exc()}"
        print(f"[run_backtest] run {run_id} FAILED:\n{err}", flush=True)
        try:
            state_store.update_run_status(run_id, "failed", error=str(e))
        except Exception as e2:
            print(f"[run_backtest] ALSO failed to record the failure itself: {e2}", flush=True)
        raise  # non-zero exit / visible traceback either way


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Top-level safety net — guarantees a traceback is printed even if
        # something above main()'s own try/except (the clone, pip install,
        # or kaggle_secrets import) is what actually failed. This is
        # exactly the class of failure that left Kaggle's UI showing
        # nothing at all before (see this file's module docstring).
        traceback.print_exc()
        sys.exit(1)
