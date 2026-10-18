"""
kaggle_backtest/run_backtest.py — runs on Kaggle, triggered by the UI's
"Run Backtest" button via api/index.py's POST /api/backtests endpoint.

See KAGGLE.md for full setup. Short version of what this script does:

1. Clones the PUBLIC Pyrex611/pyq-p3 repo (always the latest code — this
   script never needs to be re-pushed when walkforward.py itself changes,
   only when THIS orchestration script changes).
2. Installs the repo's dependencies.
3. Copies this kernel's attached Kaggle Dataset (the historical OHLCV
   CSVs — see KAGGLE.md for how that dataset is created/refreshed) into
   the cloned repo as btc_back366.csv/eth_back366.csv, where
   walkforward.py expects to find them.
4. Reads params.json (written into this kernel's own files by the API at
   push time — NOT committed to git, it's per-run) for this run's id and
   any walkforward.py module-constant overrides (days, trials, leverage,
   bayesian on/off).
5. Reads DATABASE_URL from a Kaggle Secret and marks the run "running"
   in pyquant_runs. The secret is attached ONCE, by hand, in the Kaggle
   web editor (Add-ons -> Secrets — see KAGGLE.md Part D) to this exact
   kernel (username/pyquant-backtest-runner). That's why
   api/index.py's _push_kaggle_kernel() always pushes a new VERSION of
   this same kernel id rather than a fresh slug per run: Kaggle attaches
   a secret to a kernel, not to one version of it, so only the
   always-same-slug approach keeps the attachment working across runs
   triggered with no human in the Kaggle editor.
6. Overrides walkforward.py's module constants, sets
   _SUPPRESS_AUTO_RECORD_RUN=True (see that flag's definition in
   walkforward.py for why), and calls run_walk_forward().
7. Marks the run "completed" (with metrics) or "failed" (with the error)
   in pyquant_runs — the UI picks this up via GET /api/runs/{id}, same
   as any other run.

Every step after the clone is wrapped so a failure anywhere still reports
"failed" with a real error message instead of leaving the run stuck on
"queued"/"running" forever.
"""
import json
import os
import subprocess
import sys
import traceback

REPO_DIR = "/kaggle/working/pyq-p3"
PARAMS_PATH = os.path.join(os.path.dirname(__file__), "params.json")


def _run(cmd: list) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def main() -> None:
    with open(PARAMS_PATH) as f:
        params = json.load(f)
    run_id = params["run_id"]

    # Clone (or reuse, if this cell is re-run interactively) the public repo.
    if not os.path.isdir(REPO_DIR):
        _run(["git", "clone", "--depth", "1",
              "https://github.com/Pyrex611/pyq-p3.git", REPO_DIR])
    sys.path.insert(0, REPO_DIR)

    _run([sys.executable, "-m", "pip", "install", "-q", "-r",
          os.path.join(REPO_DIR, "requirements.txt")])

    # DATABASE_URL must be set before pyquant_state.py's first call — it's
    # read lazily (os.environ.get) inside that module, not at import time,
    # so this is safe to do right before the first state_store call below.
    from kaggle_secrets import UserSecretsClient
    os.environ["DATABASE_URL"] = UserSecretsClient().get_secret("DATABASE_URL")

    import pyquant_state as state_store

    try:
        state_store.update_run_status(run_id, "running")

        # Historical data: copy this kernel's attached dataset into the
        # cloned repo, matching the filenames walkforward.py's module-level
        # BTC_CSV/ETH_CSV constants expect. KAGGLE_DATASET_SLUG comes from
        # params.json (the API fills it in from the KAGGLE_DATASET_SLUG
        # env var at push time) so this script never hardcodes a username.
        dataset_dir = f"/kaggle/input/{params['dataset_slug']}"
        import shutil
        shutil.copy(os.path.join(dataset_dir, "btc_back366.csv"), os.path.join(REPO_DIR, "btc_back366.csv"))
        shutil.copy(os.path.join(dataset_dir, "eth_back366.csv"), os.path.join(REPO_DIR, "eth_back366.csv"))

        os.chdir(REPO_DIR)
        import walkforward

        # Module-constant overrides — see CLAUDE.md Section 6.1 for why
        # this approach (not a signature refactor of run_walk_forward())
        # was chosen: it's zero-risk to the validated backtest engine's
        # internals, which read these same names throughout.
        if params.get("walk_forward_days") is not None:
            walkforward.WALK_FORWARD_DAYS = int(params["walk_forward_days"])
        if params.get("bayesian_trials") is not None:
            walkforward.BAYESIAN_TRIALS = int(params["bayesian_trials"])
        if params.get("use_bayesian") is not None:
            walkforward.USE_BAYESIAN = bool(params["use_bayesian"])
        if params.get("leverage") is not None:
            walkforward.LEVERAGE = float(params["leverage"])
        if params.get("use_atr_sl") is not None:
            walkforward.USE_ATR_SL = bool(params["use_atr_sl"])

        walkforward._SUPPRESS_AUTO_RECORD_RUN = True

        metrics, all_trades, capital_curve, params_log = walkforward.run_walk_forward()
        metrics_dict = dict(zip(metrics["Metric"], metrics["Value"])) if not metrics.empty else {}
        metrics_dict["trade_count"] = len(all_trades)

        state_store.update_run_status(run_id, "completed", metrics=metrics_dict)
        print(f"[run_backtest] run {run_id} completed: {metrics_dict}")

    except Exception as e:
        err = f"{e}\n{traceback.format_exc()}"
        print(f"[run_backtest] run {run_id} FAILED:\n{err}")
        try:
            state_store.update_run_status(run_id, "failed", error=str(e))
        except Exception:
            pass  # the run stays "running" in the UI — still better than crashing silently with no trace
        raise


if __name__ == "__main__":
    main()
