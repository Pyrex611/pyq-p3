"""
api/index.py — PyQuant Control Center backend.

Phase 2 (UI build). A FastAPI app, deployed as a single Vercel Python
serverless function (Vercel's documented convention: one `app` ASGI object
per file under api/, see vercel.json's rewrite in this same directory tree).

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

Local dev:  uvicorn api.index:app --reload --port 8000
Vercel:     this file + vercel.json + requirements.txt is the whole deploy.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# pyquant_state.py lives at the repo root, one level up from api/. Vercel's
# Python runtime invokes this file with the repo root on sys.path already;
# this line is for local `uvicorn api.index:app` dev only.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
