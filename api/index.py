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
    return {
        "cron": cron,
        "cron_age_seconds": age_seconds,
        "signal_diagnostics": diagnostics or {},
    }


# ── Runs (Backtest screen) ───────────────────────────────────────────────

@app.get("/api/runs")
def get_runs(
    run_type: Optional[str] = Query(None, description='"walkforward" or "daily_optimizer"'),
    limit: int = Query(50, ge=1, le=200),
):
    return {"runs": state_store.list_runs(run_type=run_type, limit=limit)}


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    run = state_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


# ── Backtests (Kaggle integration — Backtest screen's "Run" button) ─────
#
# See KAGGLE.md for full setup/rationale. Short version: Vercel has
# neither the compute time nor a durable filesystem for walkforward.py's
# UKF-training + 150-trial Optuna search, so this endpoint doesn't run it
# itself — it writes an immediate "queued" pyquant_runs row (so the UI has
# something to show right away) and triggers a Kaggle kernel run with
# this run's id + params baked in. The kernel (kaggle_backtest/
# run_backtest.py) updates that same row to "running" then "completed"/
# "failed" as it progresses — the UI just keeps polling GET
# /api/runs/{id}, exactly like any other run.

@app.post("/api/backtests")
def create_backtest(body: BacktestRequest):
    kaggle_username = os.environ.get("KAGGLE_USERNAME", "").strip()
    kaggle_key = os.environ.get("KAGGLE_KEY", "").strip()
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
    if not (kaggle_username and kaggle_key and dataset_slug):
        raise HTTPException(
            status_code=503,
            detail=(
                "Kaggle isn't configured on this deployment (missing "
                "KAGGLE_USERNAME / KAGGLE_KEY / KAGGLE_DATASET_SLUG env vars) "
                "— see KAGGLE.md Part C."
            ),
        )

    params = body.model_dump(exclude_none=True)
    run_id = state_store.request_backtest(run_type="walkforward", params=params, symbol="BTC+ETH")
    if run_id is None:
        raise HTTPException(status_code=500, detail="Could not create the queued run (state store write failed)")

    try:
        _push_kaggle_kernel(run_id, params, kaggle_username, kaggle_key, dataset_slug)
    except Exception as e:
        # The queued row stays "queued" forever if we don't mark it —
        # fail it immediately so the UI shows a real error instead of a
        # run that silently never progresses.
        state_store.update_run_status(run_id, "failed", error=f"Kaggle push failed: {e}")
        raise HTTPException(status_code=502, detail=f"Could not start the Kaggle run: {e}")

    return {"run_id": run_id, "status": "queued"}


def _push_kaggle_kernel(run_id: int, params: dict, username: str, key: str, dataset_slug: str) -> None:
    """Copies the kaggle_backtest/ template into a temp dir with this
    run's params.json baked in, points kernel-metadata.json at the
    configured dataset, and pushes a NEW VERSION of the SAME kernel slug
    every time (not a new slug per run) — deliberately, so the DATABASE_URL
    Kaggle Secret attached once, by hand, in the Kaggle web editor (see
    KAGGLE.md Part D) stays attached across every push. Kaggle attaches a
    secret to a kernel, not to one specific version of it, so pushing new
    code/params to the same existing kernel id keeps that attachment —
    pushing under a fresh slug per run would NOT (a brand-new kernel has
    no secrets attached until someone opens it in the editor and toggles
    one on, which defeats the point of triggering runs with no human in
    the loop). The practical corollary: only one backtest can usefully be
    in flight at a time — a second push while one is still running starts
    a new version of the same kernel, and KAGGLE.md documents this as a
    known, accepted limitation rather than something this code tries to
    queue or serialize itself.

    Raises on any failure — the caller above turns that into a failed run
    + a 502, rather than leaving the run stuck."""
    import json
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
        shutil.copy(template_dir / "run_backtest.py", os.path.join(tmp, "run_backtest.py"))

        with open(template_dir / "kernel-metadata.json") as f:
            meta = json.load(f)
        meta["id"] = f"{username}/pyquant-backtest-runner"
        meta["dataset_sources"] = [dataset_slug]
        with open(os.path.join(tmp, "kernel-metadata.json"), "w") as f:
            json.dump(meta, f)

        run_params = dict(params)
        run_params["run_id"] = run_id
        run_params["dataset_slug"] = dataset_slug
        with open(os.path.join(tmp, "params.json"), "w") as f:
            json.dump(run_params, f)

        api = KaggleApi()
        api.authenticate()
        api.kernels_push(tmp)


# ── Dashboard summary ────────────────────────────────────────────────────

@app.get("/api/dashboard")
def get_dashboard():
    """One combined payload for the Main/Dashboard screen so the frontend
    doesn't have to make four round trips on first paint."""
    open_signals = state_store.list_signals(status="open", limit=50)
    recent_closed = [
        s for s in state_store.list_signals(limit=50)
        if s["status"] not in ("open",)
    ][:10]
    runs = state_store.list_runs(limit=1)
    realized_pnl_30 = sum(
        (s.get("realized_pnl") or 0.0)
        for s in state_store.list_signals(limit=200)
        if s["status"] not in ("open",)
    )
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
        "cron_last_tick_at": cron.get("ts") if cron else None,
        "cron_age_seconds": cron_age_seconds,
    }


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
