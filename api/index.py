"""
api/index.py — PyQuant Control Center backend + frontend, one Vercel Function.

Phase 2 (UI build). A FastAPI app, deployed via Vercel's Python FRAMEWORK
PRESET (not the older "each file under api/ is its own function" model —
see the redeploy note in the repo's VERCEL_DEPLOY.md for why that version
404'd on every path). The project root's `pyproject.toml` sets
`[tool.vercel].entrypoint = "api.index:app"`, which tells Vercel: detect
FastAPI, load this exact module/variable, and route EVERY request to it —
so this one app has to serve both the JSON API AND the static frontend.
That's what the `StaticFiles` mount at the bottom of this file does: every
`@app.get("/api/...")` route below is matched first (Starlette checks
routes in registration order), and anything else falls through to the
mount, which serves `web/`'s HTML/CSS/JS. This is the exact same
app-plus-StaticFiles-mount shape already verified locally in this project's
testing (TestClient + a headless-browser pass against a combined server) —
production now matches that proven layout instead of introducing a new one.

Design choice worth stating explicitly: this API is a READ layer (plus one
narrow, deliberately-scoped write: the trading on/off switch) over
pyquant_state.py's SQLite/Postgres store — the same store pyq_p4.py,
p4_utils.py, walkforward.py and p4_orchestra.py already write to as part of
Phase 1/2. It does NOT hold Binance credentials and does NOT call the
exchange directly. Two reasons:

  1. Safety: this process runs on Vercel, separate from the orchestra
     process that actually trades. Giving a public-facing API direct
     mainnet trading credentials would widen the live trading engine's
     attack surface for no benefit — everything the UI needs (open
     positions, recent signals, run history, which mode is active) is
     already being logged to the shared state store by the trading engine
     itself as of this phase's wiring.
  2. Scope: "testing deferred to integration" / "don't touch the live
     engine casually" has been the standing rule this whole project — this
     file only reads what the engine already writes, plus the one gated,
     intentionally-safe control described in pyquant_state.py's
     is_trading_enabled()/set_trading_enabled() docstring (OFF only stops
     NEW entries; it never touches an already-open position's protection).

Local dev:  uvicorn api.index:app --reload --port 8000  (run from the repo root)
Vercel:     this file + pyproject.toml is the whole deploy. vercel.json is
            deliberately empty ({}) — a "functions"/"rewrites" block there
            is the legacy file-based-function config model, and mixing it
            with the framework-preset entrypoint above is exactly what
            caused the earlier 404 (see VERCEL_DEPLOY.md).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import traceback

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# pyquant_state.py (and the web/ directory, mounted below) live at the repo
# root, one level up from api/. Vercel's Python runtime sets the working
# directory to the project root already (see "Reading relative files" in
# Vercel's Python runtime docs), but this sys.path line makes `uvicorn
# api.index:app` work identically when run locally from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import pyquant_state as state_store  # noqa: E402

app = FastAPI(title="PyQuant Control Center API", version="1.0.0")

# The frontend (Phase 2's deployable static site) calls this API cross-origin
# from its own Vercel project/domain in the common deployment layout (two
# separate Vercel projects: one static, one this API) — same-origin deploys
# work fine too since CORS is a no-op same-origin. Tightened to a specific
# origin via the FRONTEND_ORIGIN env var once that URL is known; "*" is the
# permissive default so local development isn't blocked before it's set.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("FRONTEND_ORIGIN", "*")],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# Found during the 2026-10-20 "502 on /api/backtests with no detail"
# investigation: an uncaught exception that reaches ASGI's own default
# error handling returns an EMPTY body (not a JSON {"detail": ...}),
# which is what made that 502 undiagnosable from the frontend AND from a
# plain GET of the endpoint — there was nothing to read. This repo's own
# `HTTPException(...)` raises (e.g. create_backtest()'s 502/503) already
# carry a real `detail` string and are unaffected by this handler (FastAPI
# routes HTTPException to its own handler first); this one only catches
# whatever still escapes uncaught, and turns it into the same shape
# (`{"detail": "..."}`) instead of a silent crash. Safe to expose here:
# this API never holds exchange credentials (Section 4 of SPEC.md), so a
# stack trace from it can't leak a secret the way one from the engine
# could.
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
        },
    )


# ── Schemas ──────────────────────────────────────────────────────────────

class TradingSwitch(BaseModel):
    enabled: bool


class BacktestRequest(BaseModel):
    """Params the Backtest page's form submits. All optional — a field
    left out means "use walkforward.py's own default for that constant"
    (see kaggle_backtest/run_backtest.py, which only overrides a
    walkforward.py module constant when the matching field here is not
    None)."""
    walk_forward_days: Optional[int] = None
    bayesian_trials: Optional[int] = None
    use_bayesian: Optional[bool] = None
    leverage: Optional[float] = None
    use_atr_sl: Optional[bool] = None


# ── Health ───────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    """Unlike every data endpoint below, this one reports a real DB
    connectivity failure instead of masking it — see
    pyquant_state.db_ping()'s docstring for why that distinction matters
    (added 2026-10-20 after a suspected Vercel-vs-GitHub-Actions
    DATABASE_URL mismatch looked identical to "no data yet" everywhere
    else). Check this first whenever the UI looks disconnected."""
    db = state_store.db_ping()
    return {"ok": db.get("ok", False), "db": db}


# ── Status / mode (Dashboard + ApiKeys screens) ─────────────────────────

@app.get("/api/status")
def get_status():
    """
    Current trading on/off state plus which exchange mode the orchestra
    process is running under. `mode` is read from this API process's own
    PYQUANT_MODE env var as an informational mirror of the orchestra's
    configuration — changing it here does NOT change the orchestra's
    live/paper credentials (see pyquant_state.py's docstring); only
    `trading_enabled` is a real, live-effective control from this API.
    """
    return {
        "trading_enabled": state_store.is_trading_enabled(),
        "mode": os.environ.get("PYQUANT_MODE", "live").strip().lower(),
    }


@app.post("/api/status")
def set_status(body: TradingSwitch):
    state_store.set_trading_enabled(body.enabled)
    return {
        "trading_enabled": state_store.is_trading_enabled(),
        "mode": os.environ.get("PYQUANT_MODE", "live").strip().lower(),
    }


@app.get("/api/config")
def get_config():
    """
    Masked configuration presence check for the "API Keys & Settings"
    screen — booleans only, never the actual secret values (this endpoint
    reads THIS process's own env vars, i.e. whatever is set in the Vercel
    project's Environment Variables, not the orchestra VPS's .env file).
    """
    keys = [
        "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
        "BYBIT_API_KEY", "BYBIT_SECRET_KEY",
        "BINANCE_API_KEY", "BINANCE_SECRET_KEY",
        "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_SECRET_KEY",
        "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
        "DATABASE_URL",
    ]
    return {k: bool(os.environ.get(k, "").strip()) for k in keys}


# ── Signals (Signals + Live Monitor screens) ────────────────────────────

@app.get("/api/signals")
def get_signals(
    symbol: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    return {"signals": state_store.list_signals(symbol=symbol, status=status, limit=limit)}


@app.get("/api/signals/open")
def get_open_signals():
    """Shorthand for the Live Monitor screen's "currently open" table."""
    return {"signals": state_store.list_signals(status="open", limit=50)}


# ── Signals baseline ("fresh start") — 2026-10-26 ────────────────────────
#
# A soft, non-destructive marker: GET /api/dashboard (and GET /api/compare
# below) exclude anything opened before this timestamp from their
# figures, but nothing is deleted — the Signals page (GET /api/signals)
# always shows full history regardless. Added after the TP/SL
# close-tracking fix (PLAN.md 2026-10-25) closed a backlog of old
# signals whose PnL numbers predate that fix, and the owner asked for
# the system to "start fresh" going forward without losing that history.

@app.get("/api/signals/baseline")
def get_signals_baseline():
    return {"reset_at": state_store.get_signals_baseline()}


@app.post("/api/signals/baseline/reset")
def reset_signals_baseline():
    ts = state_store.set_signals_baseline()
    return {"reset_at": ts}


@app.post("/api/signals/baseline/clear")
def clear_signals_baseline():
    state_store.clear_signals_baseline()
    return {"reset_at": None}


# ── Heartbeat / engine-liveness diagnostics (Live Monitor screen) ───────
#
# Signal-visibility audit (2026-10-19/20): before this endpoint existed,
# the only proof the UI had that the engine was connected at all was
# /api/config's boolean "is this env var set" check — on an hours-long
# stretch with zero fired signals (expected strategy behavior; see
# PLAN.md/DOCS.md), that's indistinguishable from the cron job not running
# at all. This endpoint surfaces two things cron_runner.py/pyq_p4.py now
# persist on every tick regardless of outcome: (a) a plain "a tick
# happened at time T" heartbeat, and (b) per-symbol signal-generation
# diagnostics — trend, pct_diff/vol vs. their thresholds, and the
# entry-direction "strict" gate with how close price came to clearing it.
# Neither of these changes which signals fire; they only make the existing
# decision visible.
@app.get("/api/heartbeat")
def get_heartbeat():
    cron = state_store.get_json("pyq_p4_cron_heartbeat")
    diagnostics = state_store.get_json("pyq_p4_signal_diagnostics")
    now = datetime.now(timezone.utc)
    age_seconds = None
    if cron and cron.get("ts"):
        try:
            last_ts = datetime.fromisoformat(cron["ts"])
            age_seconds = (now - last_ts).total_seconds()
        except Exception:
            age_seconds = None

    # 2026-10-27: surfaces p4_orchestra.py's daily UKF re-anchor result
    # (pyquant_ukf_retrain_heartbeat, written by _run_daily_ukf_retrain())
    # so "is the UKF actually being retrained" is answerable from the UI
    # instead of a GitHub Actions log dig — see PLAN.md's same-dated entry
    # for the "no new signal for 3 days" root cause this was added for
    # (a slow param-search timing out the job before the re-anchor ran).
    ukf_retrain = state_store.get_json("pyquant_ukf_retrain_heartbeat")
    ukf_retrain_age_seconds = None
    if ukf_retrain and ukf_retrain.get("ts"):
        try:
            ukf_retrain_age_seconds = (now - datetime.fromisoformat(ukf_retrain["ts"])).total_seconds()
        except Exception:
            ukf_retrain_age_seconds = None

    return {
        "cron": cron,
        "cron_age_seconds": age_seconds,
        "signal_diagnostics": diagnostics or {},
        "ukf_retrain": ukf_retrain,
        "ukf_retrain_age_seconds": ukf_retrain_age_seconds,
    }


# ── Runs (Backtest screen) ───────────────────────────────────────────────

@app.get("/api/runs")
def get_runs(
    run_type: Optional[str] = Query(None, description='"walkforward" or "daily_optimizer"'),
    limit: int = Query(50, ge=1, le=200),
    include_failed: bool = Query(
        False,
        description="2026-10-25: failed backtest/optimizer runs are hidden from the "
                     "Backtest screen by default (the user's explicit request after a "
                     "Kaggle-push failure left a 'failed' row sitting in the runs list "
                     "next to real results) — pass true to see them, e.g. for debugging.",
    ),
):
    # Over-fetch before filtering so a page of `limit` real runs isn't
    # silently shrunk by however many failed ones happened to be mixed
    # in among the most recent rows.
    runs = state_store.list_runs(run_type=run_type, limit=limit if include_failed else limit * 2)
    if not include_failed:
        runs = [r for r in runs if r.get("status") != "failed"][:limit]
    return {"runs": runs}


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    run = state_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/runs/{run_id}/trades")
def get_run_trades(run_id: int):
    """2026-10-28: per-trade data for one backtest run — see
    pyquant_state.record_backtest_trades()'s docstring. Not yet
    rendered anywhere in the UI; this is the foundation a future
    Backtest-vs-Live per-trade comparison reads from (PLAN.md's
    2026-10-27 Decisions entry). Returns an empty list (not a 404) for
    a run with no persisted trades — covers both "run predates this
    feature" and "run_type isn't a backtest" without the caller having
    to special-case either."""
    return {"run_id": run_id, "trades": state_store.get_backtest_trades(run_id)}


# ── Backtests (Backtest screen's "Run" button) ──────────────────────────
#
# See KAGGLE.md for full setup/rationale. Short version: Vercel has
# neither the compute time nor a durable filesystem for walkforward.py's
# UKF-training + 150-trial Optuna search, so this endpoint doesn't run it
# itself — it writes an immediate "queued" pyquant_runs row (so the UI has
# something to show right away) and triggers the run elsewhere, which
# updates that same row to "running" then "completed"/"failed" as it
# progresses — the UI just keeps polling GET /api/runs/{id}, exactly like
# any other run.
#
# 2026-10-25: switched the trigger from a Kaggle kernel push
# (_push_kaggle_kernel(), kept below, unused, as a documented fallback —
# see KAGGLE.md's "GitHub Actions alternative" section for why) to a
# GitHub Actions workflow_dispatch (_trigger_github_backtest()). Root
# cause of the switch: even after fixing the kernel-type 409 Conflict, a
# subsequent real backtest attempt failed again with Kaggle's own UI
# showing no error log at all — nothing programmatic to inspect from
# this session, and nothing for the owner to act on either. GitHub
# Actions' log viewer has already proven reliable for this exact
# dependency stack (orchestra.yml's cron tick installs the same
# requirements.txt successfully every ~5 minutes), so moving backtest
# EXECUTION there — while still pulling historical data from the
# existing Kaggle DATASET via a plain `kaggle datasets download` call,
# which has none of the kernel sandbox's opacity — keeps everything that
# was already working and only replaces the part that wasn't.

@app.post("/api/backtests")
def create_backtest(body: BacktestRequest):
    dataset_slug = os.environ.get("KAGGLE_DATASET_SLUG", "").strip()
    # Found 2026-10-24: a real backtest attempt failed with Kaggle's own
    # "Invalid dataset specification" — KAGGLE_DATASET_SLUG had been set to
    # the full dataset page URL (https://www.kaggle.com/datasets/<owner>/
    # <slug>) rather than the bare "<owner>/<slug>" the API expects
    # (KAGGLE.md's own example already showed the bare form, but nothing
    # stopped the URL form from being pasted in by mistake). Rather than
    # just document it more loudly, strip a pasted URL down to the bare
    # slug here so this specific mistake can't recur.
    for _prefix in ("https://www.kaggle.com/datasets/", "http://www.kaggle.com/datasets/",
                     "www.kaggle.com/datasets/", "kaggle.com/datasets/"):
        if dataset_slug.lower().startswith(_prefix):
            dataset_slug = dataset_slug[len(_prefix):]
            break
    dataset_slug = dataset_slug.strip("/")

    github_token = os.environ.get("GITHUB_DISPATCH_TOKEN", "").strip()
    if not (github_token and dataset_slug):
        raise HTTPException(
            status_code=503,
            detail=(
                "Backtest triggering isn't configured on this deployment "
                "(missing GITHUB_DISPATCH_TOKEN and/or KAGGLE_DATASET_SLUG "
                "env vars) — see KAGGLE.md's \"GitHub Actions alternative\" "
                "section."
            ),
        )

    params = body.model_dump(exclude_none=True)
    run_id = state_store.request_backtest(run_type="walkforward", params=params, symbol="BTC+ETH")
    if run_id is None:
        raise HTTPException(status_code=500, detail="Could not create the queued run (state store write failed)")

    try:
        _trigger_github_backtest(run_id, params, dataset_slug, github_token)
    except Exception as e:
        # The queued row stays "queued" forever if we don't mark it —
        # fail it immediately so the UI shows a real error instead of a
        # run that silently never progresses.
        state_store.update_run_status(run_id, "failed", error=f"GitHub Actions dispatch failed: {e}")
        raise HTTPException(status_code=502, detail=f"Could not start the backtest workflow: {e}")

    return {"run_id": run_id, "status": "queued"}


@app.post("/api/backtests/sync-to-kaggle")
def sync_backtest_to_kaggle(body: BacktestRequest):
    """The manual-trigger half of the Kaggle path (KAGGLE.md Part K): pushes
    a new version of the same Kaggle kernel with this run's values baked
    into run_backtest.py's RUN PARAMETERS block, then returns the kernel's
    URL so the owner can open it in Kaggle's own editor and click "Save &
    Run All" themselves — full, visible console output, no GitHub Actions
    involved. Separate from POST /api/backtests (the GitHub-Actions path,
    now the default "Run Backtest" button) — this is the "Sync to Kaggle"
    button instead, for when a hands-on re-run with full visibility is
    wanted, or GitHub Actions' own path is blocked for some reason."""
    username = os.environ.get("KAGGLE_USERNAME", "").strip()
    key = os.environ.get("KAGGLE_KEY", "").strip()
    dataset_slug = os.environ.get("KAGGLE_DATASET_SLUG", "").strip()
    for _prefix in ("https://www.kaggle.com/datasets/", "http://www.kaggle.com/datasets/",
                     "www.kaggle.com/datasets/", "kaggle.com/datasets/"):
        if dataset_slug.lower().startswith(_prefix):
            dataset_slug = dataset_slug[len(_prefix):]
            break
    dataset_slug = dataset_slug.strip("/")

    if not (username and key and dataset_slug):
        raise HTTPException(
            status_code=503,
            detail=(
                "Kaggle sync isn't configured on this deployment (missing "
                "KAGGLE_USERNAME/KAGGLE_KEY/KAGGLE_DATASET_SLUG env vars) "
                "— see KAGGLE.md Part D/E."
            ),
        )

    params = body.model_dump(exclude_none=True)
    run_id = state_store.request_backtest(run_type="walkforward", params=params, symbol="BTC+ETH")
    if run_id is None:
        raise HTTPException(status_code=500, detail="Could not create the queued run (state store write failed)")

    try:
        _push_kaggle_kernel(run_id, params, username, key, dataset_slug)
    except Exception as e:
        state_store.update_run_status(run_id, "failed", error=f"Kaggle kernel push failed: {e}")
        raise HTTPException(status_code=502, detail=f"Could not push to Kaggle: {e}")

    # Stays "queued" — unlike the GitHub Actions path, nothing here starts
    # the run itself. A human (or Kaggle's own auto-run-on-push setting,
    # if the owner has turned that on) is what actually runs it next.
    kernel_slug = f"{username}/pyquant-backtest-runner"
    return {
        "run_id": run_id,
        "status": "queued",
        "kaggle_kernel_url": f"https://www.kaggle.com/code/{kernel_slug}",
        "note": "Pushed. Open the kernel in Kaggle's editor and click \"Save & Run All\" to start it.",
    }


def _trigger_github_backtest(run_id: int, params: dict, dataset_slug: str, github_token: str) -> None:
    """Fires .github/workflows/backtest.yml's workflow_dispatch trigger via
    GitHub's REST API — see this section's header comment above for why
    this replaced a direct Kaggle kernel push. `github_token` is a
    fine-grained PAT scoped to this repo with Actions: read/write (the
    SAME kind of token GITHUB_ACTIONS_SETUP.md Part G already has the
    owner create for the cron-reliability pinger — reuse that one, or
    make a second one the same way).

    Raises on any non-204 response — the caller above turns that into a
    failed run + a 502, rather than leaving the run stuck on "queued"."""
    import requests

    workflow_dispatch_inputs = {
        "run_id": str(run_id),
        "dataset_slug": dataset_slug,
    }
    if params.get("walk_forward_days") is not None:
        workflow_dispatch_inputs["walk_forward_days"] = str(params["walk_forward_days"])
    if params.get("bayesian_trials") is not None:
        workflow_dispatch_inputs["bayesian_trials"] = str(params["bayesian_trials"])
    if params.get("leverage") is not None:
        workflow_dispatch_inputs["leverage"] = str(params["leverage"])
    if params.get("use_bayesian") is not None:
        workflow_dispatch_inputs["use_bayesian"] = bool(params["use_bayesian"])
    if params.get("use_atr_sl") is not None:
        workflow_dispatch_inputs["use_atr_sl"] = bool(params["use_atr_sl"])

    resp = requests.post(
        "https://api.github.com/repos/Pyrex611/pyq-p3/actions/workflows/backtest.yml/dispatches",
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        json={"ref": "main", "inputs": workflow_dispatch_inputs},
        timeout=15,
    )
    if resp.status_code != 204:
        raise RuntimeError(f"GitHub API returned {resp.status_code}: {resp.text[:500]}")


# 2026-10-26: this is the "manual-trigger" path's push half — the owner's
# ask was "paste it in Kaggle manually... OR have it automatically put the
# code there like it does currently" (PLAN.md/KAGGLE.md Part K). It stopped
# being create_backtest()'s own trigger on 2026-10-25 (GitHub Actions is the
# default execution path now — see that function's header comment above),
# but it's very much alive again as what POST /api/backtests/sync-to-kaggle
# (below) calls: push fresh values into the SAME kernel slug, then the
# owner opens Kaggle's own editor and clicks "Save & Run All" for full,
# visible console output — or lets Kaggle's own auto-run-on-push behavior
# start it. Rewritten 2026-10-26 to match kaggle_backtest/run_backtest.py's
# new design: that script no longer reads a separate params.json (which
# was invisible unless you went looking for it, and not hand-editable
# before a manual re-run) — it reads inline module-level constants instead,
# between the `# ── RUN PARAMETERS ──` / `# ── END RUN PARAMETERS ──`
# marker comments. This function now does the equivalent of hand-editing
# those lines: read the template, string-replace that exact block with one
# baking in this run's real values, then push the result. If those marker
# strings are ever renamed in run_backtest.py, update _RUN_PARAMS_START/
# _RUN_PARAMS_END below in the same change — they must match verbatim.
def _push_kaggle_kernel(run_id: int, params: dict, username: str, key: str, dataset_slug: str) -> None:
    """Copies the kaggle_backtest/ template into a temp dir with this run's
    parameters baked directly into its RUN PARAMETERS block (see header
    comment above), points kernel-metadata.json at the configured dataset,
    and pushes a NEW VERSION of the SAME kernel slug every time (not a new
    slug per run) — deliberately, so the DATABASE_URL Kaggle Secret
    attached once, by hand, in the Kaggle web editor (see KAGGLE.md Part D)
    stays attached across every push. Kaggle attaches a secret to a
    kernel, not to one specific version of it, so pushing new code to the
    same existing kernel id keeps that attachment — pushing under a fresh
    slug per run would NOT (a brand-new kernel has no secrets attached
    until someone opens it in the editor and toggles one on, which defeats
    the point of triggering runs with no human in the loop). The practical
    corollary: only one backtest can usefully be in flight at a time — a
    second push while one is still running starts a new version of the
    same kernel, and KAGGLE.md documents this as a known, accepted
    limitation rather than something this code tries to queue or
    serialize itself.

    Raises on any failure — the caller below turns that into a failed run
    + a 502, rather than leaving the run stuck."""
    import json
    import re
    import shutil
    import tempfile

    os.environ.setdefault("KAGGLE_USERNAME", username)
    os.environ.setdefault("KAGGLE_KEY", key)

    # Found 2026-10-20 via a live 502's error detail (surfaced only after
    # that turn's apiGet/apiPost fix — see PLAN.md): "[Errno 30] Read-only
    # file system: '/home/sbx_user1051'". That path is $HOME inside
    # Vercel's Python sandbox, and it's read-only — only /tmp is writable.
    # Even with KAGGLE_USERNAME/KAGGLE_KEY set as env vars, the kaggle
    # package's KaggleApi still resolves (and on some code paths tries to
    # create) a config directory that defaults to `~/.kaggle`, i.e.
    # os.path.expanduser("~")/.kaggle — which is that read-only $HOME.
    # Fix: point KAGGLE_CONFIG_DIR at a directory under /tmp (the one
    # writable path in this sandbox) and pre-create it with a kaggle.json
    # written from the same credentials, so every code path the library
    # might take — env-var lookup or config-file lookup — resolves
    # successfully regardless of which one this installed version
    # actually uses internally.
    _kaggle_config_dir = "/tmp/.kaggle"
    os.makedirs(_kaggle_config_dir, exist_ok=True)
    os.environ["KAGGLE_CONFIG_DIR"] = _kaggle_config_dir
    _kaggle_json_path = os.path.join(_kaggle_config_dir, "kaggle.json")
    with open(_kaggle_json_path, "w") as f:
        json.dump({"username": username, "key": key}, f)
    os.chmod(_kaggle_json_path, 0o600)  # the kaggle CLI/library warns (non-fatally) if this is left world-readable

    from kaggle.api.kaggle_api_extended import KaggleApi  # local import: optional dep, see KAGGLE.md

    template_dir = _REPO_ROOT / "kaggle_backtest"
    with tempfile.TemporaryDirectory() as tmp:
        with open(template_dir / "run_backtest.py") as f:
            script = f.read()
        script = _render_run_params_block(script, run_id, params, dataset_slug)
        with open(os.path.join(tmp, "run_backtest.py"), "w") as f:
            f.write(script)

        with open(template_dir / "kernel-metadata.json") as f:
            meta = json.load(f)
        meta["id"] = f"{username}/pyquant-backtest-runner"
        meta["dataset_sources"] = [dataset_slug]
        with open(os.path.join(tmp, "kernel-metadata.json"), "w") as f:
            json.dump(meta, f)

        api = KaggleApi()
        api.authenticate()
        api.kernels_push(tmp)


_RUN_PARAMS_START = "# ── RUN PARAMETERS "
_RUN_PARAMS_END = "# ── END RUN PARAMETERS "


def _render_run_params_block(script: str, run_id: int, params: dict, dataset_slug: str) -> str:
    """String-replaces run_backtest.py's RUN PARAMETERS block (the lines
    between the `# ── RUN PARAMETERS ──` / `# ── END RUN PARAMETERS ──`
    marker comments) with one baking in this run's real values — the
    automated-push equivalent of hand-editing those same constants in
    Kaggle's own editor. Raises ValueError if the markers aren't found
    (fail loudly rather than silently pushing the template's placeholder
    defaults — exactly the bug this rewrite exists to prevent)."""
    import re

    start_idx = script.find(_RUN_PARAMS_START)
    end_idx = script.find(_RUN_PARAMS_END)
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        raise ValueError(
            "run_backtest.py's RUN PARAMETERS markers were not found — "
            "template may have been edited; _render_run_params_block() "
            "needs updating to match (see api/index.py's comment above "
            "_push_kaggle_kernel())."
        )
    # end_idx currently points at the start of the END marker's own line;
    # walk it back to the end of the PREVIOUS line so that line (the last
    # real parameter assignment) is kept, and walk start_idx forward past
    # its own marker line so that one is replaced along with the body.
    block_start = script.find("\n", start_idx) + 1
    block_end = end_idx

    def _lit(key: str):
        v = params.get(key)
        if v is None:
            return "None"
        if isinstance(v, bool):
            return "True" if v else "False"
        if isinstance(v, (int, float)):
            return repr(v)
        return repr(str(v))

    new_block = (
        f"RUN_ID = {run_id!r}\n"
        f"WALK_FORWARD_DAYS = {_lit('walk_forward_days')}\n"
        f"BAYESIAN_TRIALS = {_lit('bayesian_trials')}\n"
        f"LEVERAGE = {_lit('leverage')}\n"
        f"USE_BAYESIAN = {_lit('use_bayesian')}\n"
        f"USE_ATR_SL = {_lit('use_atr_sl')}\n"
        f"DATASET_SLUG = {dataset_slug!r}\n"
    )
    return script[:block_start] + new_block + script[block_end:]


# ── Dashboard summary ────────────────────────────────────────────────────

@app.get("/api/dashboard")
def get_dashboard():
    """One combined payload for the Main/Dashboard screen so the frontend
    doesn't have to make four round trips on first paint."""
    # 2026-10-26 ("fresh start"): if a signals baseline has been set
    # (POST /api/signals/reset-baseline), everything opened before it is
    # excluded from every figure below — it still exists (Signals page,
    # GET /api/compare), this is a display filter, not a delete. See
    # pyquant_state.py's get_signals_baseline() docstring for why.
    baseline = state_store.get_signals_baseline()

    def _after_baseline(s: dict) -> bool:
        if not baseline or not s.get("opened_at"):
            return True
        try:
            return datetime.fromisoformat(s["opened_at"]) >= datetime.fromisoformat(baseline)
        except Exception:
            return True

    open_signals = [s for s in state_store.list_signals(status="open", limit=50) if _after_baseline(s)]
    recent_closed = [
        s for s in state_store.list_signals(limit=50)
        if s["status"] not in ("open",) and _after_baseline(s)
    ][:10]
    runs = state_store.list_runs(limit=1)
    # 2026-10-25 (PnL-by-percentage): a summed dollar PnL is meaningless
    # under SIGNAL_ONLY_MODE (no real wallet balance — see PLAN.md's
    # 2026-10-25 entry on the TP/SL close-tracking bug this is paired
    # with), so the headline figure is now the AVERAGE leveraged
    # percentage return across recently-closed signals that have one
    # (realized_pnl_pct; older rows closed before this field existed
    # don't, and are excluded from the average rather than counted as
    # 0%). realized_pnl (USD) is kept as a secondary figure for paper/live
    # rows where a real balance delta is meaningful.
    perf = state_store.compute_live_performance(since=baseline, limit=200)
    closed_recent_all = [
        s for s in state_store.list_signals(limit=200)
        if s["status"] not in ("open",) and _after_baseline(s)
    ]
    realized_pnl_30 = sum((s.get("realized_pnl") or 0.0) for s in closed_recent_all)
    # Signal-visibility audit (2026-10-19/20): include a thin heartbeat
    # summary here too (full detail is GET /api/heartbeat) so the main
    # Dashboard screen itself shows engine-liveness on first paint, not
    # just the Live Monitor screen.
    cron = state_store.get_json("pyq_p4_cron_heartbeat")
    cron_age_seconds = None
    if cron and cron.get("ts"):
        try:
            cron_age_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(cron["ts"])).total_seconds()
        except Exception:
            cron_age_seconds = None
    return {
        "trading_enabled": state_store.is_trading_enabled(),
        "mode": os.environ.get("PYQUANT_MODE", "live").strip().lower(),
        "open_positions": open_signals,
        "recent_closed": recent_closed,
        "last_run": runs[0] if runs else None,
        "realized_pnl_recent": realized_pnl_30,
        "avg_pnl_pct_recent": perf["avg_pnl_pct"],
        "win_rate_pct_recent": perf["win_rate_pct"],
        "signals_baseline_at": baseline,
        "cron_last_tick_at": cron.get("ts") if cron else None,
        "cron_age_seconds": cron_age_seconds,
    }


# ── Backtest-vs-Live comparison (new Compare screen) — 2026-10-26 ───────
#
# v1, aggregate-level only: the latest completed walkforward run's own
# metrics (win rate %, total return %, trade count — whatever
# walkforward.py's build_metrics_table() produced, as formatted strings)
# placed next to a live/simulated performance summary computed the exact
# same way GET /api/dashboard does (state_store.compute_live_performance()),
# respecting the signals baseline above. This deliberately does NOT do
# trade-by-trade reconciliation (matching a specific live signal to a
# specific walkforward trade) — see PLAN.md's 2026-10-26 "brainstorm"
# section for why that's a bigger, separate piece of work (walkforward.py
# currently only returns trade_count, not the trade log itself, to
# pyquant_runs — persisting that is the prerequisite).

@app.get("/api/compare")
def get_compare():
    baseline = state_store.get_signals_baseline()
    live = state_store.compute_live_performance(since=baseline, limit=500)

    wf_runs = state_store.list_runs(run_type="walkforward", limit=1)
    latest_wf = wf_runs[0] if wf_runs and wf_runs[0].get("status") == "completed" else None

    return {
        "signals_baseline_at": baseline,
        "live": live,
        "latest_walkforward_run": latest_wf,
    }


@app.get("/api/compare/daily")
def get_compare_daily(run_id: Optional[int] = Query(None, description="Defaults to the latest completed walkforward run")):
    """2026-10-29: the day-bucketed Backtest-vs-Live comparison —
    the concrete next step compare.html's "brainstorm" section flagged
    as what v2 needs (pyquant_backtest_trades, added 2026-10-28, is the
    prerequisite that already shipped). See
    state_store.compute_comparison_buckets()'s docstring for the
    bucketing strategy and why it's day-granularity, not a 1:1 trade
    match. Not yet a verdict on strategy parity by itself — it's the
    data a future side-by-side chart/table reads from."""
    baseline = state_store.get_signals_baseline()
    return state_store.compute_comparison_buckets(run_id=run_id, since=baseline)


# ── Static frontend ─────────────────────────────────────────────────────
#
# Registered LAST, deliberately: Starlette (FastAPI's underlying router)
# tries routes in the order they were added, so every "/api/..." route
# above is matched first. A request this mount actually receives (e.g.
# "/", "/signals.html", "/assets/app.js") is either not an API call at
# all, or an API path that genuinely doesn't exist — StaticFiles returns
# a real 404 for the latter, same as before this mount existed.
# html=True makes "/" resolve to web/index.html, matching how the Phase 2
# frontend pages link to each other (index.html, live-monitor.html, etc.)
app.mount("/", StaticFiles(directory=str(_REPO_ROOT / "web"), html=True), name="frontend")
