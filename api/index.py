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
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
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


# ── Schemas ──────────────────────────────────────────────────────────────

class TradingSwitch(BaseModel):
    enabled: bool


# ── Health ───────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"ok": True}


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
    return {
        "trading_enabled": state_store.is_trading_enabled(),
        "mode": os.environ.get("PYQUANT_MODE", "live").strip().lower(),
        "open_positions": open_signals,
        "recent_closed": recent_closed,
        "last_run": runs[0] if runs else None,
        "realized_pnl_recent": realized_pnl_30,
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
