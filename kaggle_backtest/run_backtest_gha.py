"""
kaggle_backtest/run_backtest_gha.py — GitHub Actions counterpart to
run_backtest.py (same directory), added 2026-10-25.

Why this exists alongside run_backtest.py rather than replacing it: see
.github/workflows/backtest.yml's header comment and KAGGLE.md's
"GitHub Actions alternative" section for the full incident writeup.
Short version — the Kaggle KERNEL execution path kept failing with no
visible error log on Kaggle's own UI, which made it undebuggable from
here; this script runs the exact same walkforward.py logic inside a
GitHub Actions job instead, which already has a proven-reliable log
viewer (this repo's own orchestra.yml cron workflow has been running
the same requirements.txt successfully for days). The historical-data
CSVs still come from the existing Kaggle DATASET (not kernel) — that's
a plain file download with none of the kernel sandbox's opacity, so
there was no reason to also move the data source.

Differences from run_backtest.py (which still works this same way for
the Kaggle-kernel path, kept as-is, not deleted):
- No git clone — the workflow's own `actions/checkout` step already has
  the full repo checked out at the job's working directory.
- No `kaggle_secrets.UserSecretsClient` (that's Kaggle-kernel-specific)
  — DATABASE_URL arrives as a plain env var, set from a GitHub Secret by
  the workflow file itself.
- No params.json file — every parameter arrives as a plain env var
  (RUN_ID, WALK_FORWARD_DAYS, BAYESIAN_TRIALS, LEVERAGE, USE_BAYESIAN,
  USE_ATR_SL), set from the workflow_dispatch inputs by the workflow
  file. Empty-string env vars (an input left blank) are treated the
  same as "no override" — matching params.json's `None`/missing-key
  handling in run_backtest.py.
- The historical-data CSVs are already copied into the repo root by the
  workflow's own "Download historical-data Kaggle Dataset" step, before
  this script runs — so no dataset-copy step here at all.

Every print() below is flush=True and every step is wrapped so a
failure anywhere (including ones run_backtest.py's equivalent flow
leaves unguarded, like the import/setup section) still reports "failed"
with a real error message to pyquant_runs AND to this job's own log —
this is deliberately more defensive than run_backtest.py's original
version, as a direct response to the "Kaggle says Failed with zero
detail" incident this script exists to route around. The hardening
applies equally well to debugging THIS path if it ever fails too.
"""
import os
import sys
import traceback

# 2026-10-28: fixes a real, confirmed-live ModuleNotFoundError ("No
# module named 'pyquant_state'"). `python3 kaggle_backtest/run_backtest_gha.py`
# puts the SCRIPT's own directory (kaggle_backtest/) at sys.path[0], not
# the invoking shell's cwd (the repo root, where actions/checkout put
# everything) — so `import pyquant_state`/`import walkforward`, both of
# which live at the repo root, fail no matter what the workflow's
# `working-directory` is set to. Explicitly adding the repo root (one
# level up from this file) fixes it regardless of how the script is
# invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    return default


def _env_int(name: str):
    v = os.environ.get(name, "").strip()
    return int(v) if v else None


def _env_float(name: str):
    v = os.environ.get(name, "").strip()
    return float(v) if v else None


def main() -> None:
    print("[run_backtest_gha] Starting.", flush=True)

    run_id = os.environ.get("RUN_ID", "").strip()
    if not run_id:
        raise RuntimeError("RUN_ID env var is missing or empty — the workflow_dispatch "
                            "input didn't come through. Check the triggering API call's "
                            "`inputs` payload.")
    run_id = int(run_id)
    print(f"[run_backtest_gha] run_id={run_id}", flush=True)

    print("[run_backtest_gha] Importing pyquant_state…", flush=True)
    import pyquant_state as state_store

    try:
        state_store.update_run_status(run_id, "running")
        print("[run_backtest_gha] Marked run 'running'.", flush=True)

        for fname in ("btc_back366.csv", "eth_back366.csv"):
            if not os.path.isfile(fname):
                raise FileNotFoundError(
                    f"{fname} not found in the working directory — the workflow's "
                    f"'Download historical-data Kaggle Dataset' step should have put it "
                    f"here before this script ran. Check that step's own log output."
                )
        print("[run_backtest_gha] Historical-data CSVs present.", flush=True)

        print("[run_backtest_gha] Importing walkforward…", flush=True)
        import walkforward

        wfd = _env_int("WALK_FORWARD_DAYS")
        if wfd is not None:
            walkforward.WALK_FORWARD_DAYS = wfd
        bt = _env_int("BAYESIAN_TRIALS")
        if bt is not None:
            walkforward.BAYESIAN_TRIALS = bt
        lev = _env_float("LEVERAGE")
        if lev is not None:
            walkforward.LEVERAGE = lev
        walkforward.USE_BAYESIAN = _env_bool("USE_BAYESIAN", walkforward.USE_BAYESIAN)
        walkforward.USE_ATR_SL = _env_bool("USE_ATR_SL", walkforward.USE_ATR_SL)
        walkforward._SUPPRESS_AUTO_RECORD_RUN = True

        print(
            f"[run_backtest_gha] Params: WALK_FORWARD_DAYS={walkforward.WALK_FORWARD_DAYS} "
            f"BAYESIAN_TRIALS={walkforward.BAYESIAN_TRIALS} LEVERAGE={walkforward.LEVERAGE} "
            f"USE_BAYESIAN={walkforward.USE_BAYESIAN} USE_ATR_SL={walkforward.USE_ATR_SL}",
            flush=True,
        )

        print("[run_backtest_gha] Calling walkforward.run_walk_forward()…", flush=True)
        metrics, all_trades, capital_curve, params_log = walkforward.run_walk_forward()
        metrics_dict = dict(zip(metrics["Metric"], metrics["Value"])) if not metrics.empty else {}
        metrics_dict["trade_count"] = len(all_trades)

        # 2026-10-28: persist per-trade data for this run — see
        # kaggle_backtest/run_backtest.py's matching comment and
        # PLAN.md's 2026-10-27 Decisions entry for the full rationale.
        try:
            n = state_store.record_backtest_trades(
                run_id, walkforward.trades_to_records(all_trades, walkforward.LEVERAGE)
            )
            print(f"[run_backtest_gha] Persisted {n} per-trade rows for run {run_id}.", flush=True)
        except Exception as e:
            print(f"[run_backtest_gha] (non-fatal) per-trade logging failed: {e}", flush=True)

        state_store.update_run_status(run_id, "completed", metrics=metrics_dict)
        print(f"[run_backtest_gha] run {run_id} completed: {metrics_dict}", flush=True)

    except Exception as e:
        err = f"{e}\n{traceback.format_exc()}"
        print(f"[run_backtest_gha] run {run_id} FAILED:\n{err}", flush=True)
        try:
            state_store.update_run_status(run_id, "failed", error=str(e))
        except Exception as e2:
            print(f"[run_backtest_gha] ALSO failed to record the failure itself: {e2}", flush=True)
        raise  # non-zero exit — shows as a red X in the Actions list, matching cron_runner.py's convention


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Top-level safety net: guarantees a traceback is printed even if
        # something above main()'s own try/except (the pyquant_state
        # import itself, or RUN_ID validation) is what actually failed —
        # exactly the class of failure that left Kaggle's UI showing
        # nothing (see this file's module docstring).
        traceback.print_exc()
        sys.exit(1)
