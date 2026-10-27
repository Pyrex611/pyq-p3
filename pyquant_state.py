"""
pyquant_state.py

Phase 1 (Pre-UI stabilization) — externalized state store.

Why this exists: pyq_p4.py and p4_utils.py keep several pieces of state on
local disk — `last_execution_timestamp.txt` / `minute_execution_timestamp.txt`
(duplicate-run guards), `active_trades.json` (PositionGuard's monitored
orders), and `optimal_params.json` (the daily optimizer's output). That's
fine for a single long-running process on a VPS, but it breaks under two
things this project is moving toward:

  1. Vercel's serverless functions are stateless between invocations —
     local disk writes from one invocation are not guaranteed to be there
     on the next.
  2. Upstash QStash delivers at-least-once, not exactly-once — a task can
     be triggered twice for what should be "the same hour" / "the same
     minute", and the old file-based guards used local mtime/content
     comparisons that aren't safe against two invocations racing each
     other.

This module gives both pyq_p4.py (run guards) and p4_utils.py
(PositionGuard state, optimal params) a single, atomic, externally-backed
place to read and write that state, with two backends:

  - SQLite (default) — a single file, fine for the current VPS/orchestra
    deployment, and for local development. No new infrastructure needed.
  - Postgres (set DATABASE_URL) — for the actual Vercel deployment, where
    local disk isn't durable across invocations. See the "Phase 1 — DB +
    QStash" step-by-step guide for how to provision this.

The public API is deliberately small and backend-agnostic: callers never
see SQL or a connection object.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

_DB_URL = os.environ.get("DATABASE_URL", "").strip()
_SQLITE_PATH = os.environ.get("PYQUANT_STATE_DB", "pyquant_state.sqlite3")

_lock = threading.Lock()
_pg_pool = None  # lazily created psycopg2 connection, Postgres backend only


def _using_postgres() -> bool:
    return bool(_DB_URL)


def db_ping() -> dict:
    """Unlike every other function in this module, this one does NOT
    swallow its exception — every read/write helper here (get_json(),
    list_runs(), is_trading_enabled(), etc.) is deliberately best-effort
    and returns a safe-looking default on ANY failure, by standing design
    (CLAUDE.md Section 7's "state-store writes from the engine are
    best-effort" rule, which the read side inherited too). That's correct
    for the trading engine (a logging hiccup must never block a trading
    decision) but it means a genuinely broken DATABASE_URL on the API
    side looks IDENTICAL to "legitimately no data yet" from every other
    endpoint — confirmed during the 2026-10-20 signal-visibility/
    backtest-502 investigation (`is_trading_enabled()` defaults to `True`
    on any failure; `list_runs()`/`get_json()` default to `[]`/`None`).
    This function exists specifically to answer "is the DB actually
    reachable, and if not, why" without that masking — exposed via
    `GET /api/health` (api/index.py)."""
    if not _using_postgres():
        return {"ok": True, "backend": "sqlite", "note": "DATABASE_URL is not set — using local SQLite."}
    try:
        with _pg_conn().cursor() as cur:
            # 2026-10-25: "ok": true alone wasn't enough to settle whether
            # the engine (GitHub Actions) and the API (Vercel) are
            # actually talking to the SAME database — both sides can
            # report a perfectly healthy, independently-working Postgres
            # connection while quietly pointing at two different ones.
            # current_database()/inet_server_addr()/inet_server_port()
            # are ground truth about which physical database was reached,
            # independent of what the connection string's text claims.
            # cron_runner.py calls this same function and writes these
            # same fields into its heartbeat payload (pyq_p4_cron_heartbeat)
            # — comparing GET /api/health's identity against GET
            # /api/heartbeat's "engine_db" field answers "same DB?" without
            # running anything locally (scripts/check_db_identity.py is
            # the heavier, fully independent cross-check for when this
            # in-product comparison itself is inconclusive, e.g. because
            # the engine side has never successfully written a heartbeat
            # at all).
            cur.execute("SELECT current_database(), inet_server_addr(), inet_server_port()")
            dbname, addr, port = cur.fetchone()
        return {
            "ok": True, "backend": "postgres",
            "database": dbname, "server_addr": str(addr) if addr is not None else None, "server_port": port,
        }
    except Exception as e:
        return {"ok": False, "backend": "postgres", "error": f"{type(e).__name__}: {e}"}


def _pg_conn():
    global _pg_pool
    if _pg_pool is None:
        try:
            import psycopg2  # local import: only required when DATABASE_URL is set
        except ImportError as e:
            raise ImportError(
                "DATABASE_URL is set but psycopg2 isn't installed. "
                "Run: pip install psycopg2-binary"
            ) from e
        _pg_pool = psycopg2.connect(_DB_URL)
        _pg_pool.autocommit = True
        _bootstrap_postgres(_pg_pool)
    return _pg_pool


def _bootstrap_postgres(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pyquant_kv (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pyquant_run_guard (
                task       TEXT NOT NULL,
                bucket     TEXT NOT NULL,
                claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (task, bucket)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pyquant_signals (
                id              SERIAL PRIMARY KEY,
                symbol          TEXT NOT NULL,
                side            TEXT NOT NULL,
                entry_price     DOUBLE PRECISION NOT NULL,
                sl_price        DOUBLE PRECISION,
                tp_price        DOUBLE PRECISION,
                qty             DOUBLE PRECISION,
                entry_capital   DOUBLE PRECISION,
                expected_max_loss DOUBLE PRECISION,
                mode            TEXT NOT NULL DEFAULT 'live',
                status          TEXT NOT NULL DEFAULT 'open',
                outcome_price   DOUBLE PRECISION,
                realized_pnl    DOUBLE PRECISION,
                opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                closed_at       TIMESTAMPTZ
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pyquant_runs (
                id            SERIAL PRIMARY KEY,
                run_type      TEXT NOT NULL,
                symbol        TEXT,
                params        TEXT,
                metrics       TEXT NOT NULL,
                started_at    TIMESTAMPTZ,
                finished_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # Kaggle backtest-integration addition: a run now has a lifecycle
        # (queued -> running -> completed/failed) instead of only ever
        # being written once, complete, at the end (record_run()'s
        # original and still-valid use for daily_optimizer()/manual
        # walkforward.py runs). ADD COLUMN IF NOT EXISTS is safe to repeat
        # on every startup and a no-op for rows that predate this change
        # (they default to 'completed', which is correct — they really
        # were completed runs with no queued phase).
        cur.execute("ALTER TABLE pyquant_runs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed'")
        cur.execute("ALTER TABLE pyquant_runs ADD COLUMN IF NOT EXISTS kaggle_ref TEXT")
        cur.execute("ALTER TABLE pyquant_runs ADD COLUMN IF NOT EXISTS error TEXT")
        # 2026-10-25 (PnL-by-percentage): see the matching SQLite ALTER
        # below for why this is additive/nullable rather than a
        # replacement for realized_pnl (a dollar figure) — existing rows
        # keep their old realized_pnl value, new closes get both.
        cur.execute("ALTER TABLE pyquant_signals ADD COLUMN IF NOT EXISTS realized_pnl_pct DOUBLE PRECISION")


@contextmanager
def _sqlite_conn():
    conn = sqlite3.connect(_SQLITE_PATH, timeout=10)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pyquant_kv ("
            "  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pyquant_run_guard ("
            "  task TEXT NOT NULL, bucket TEXT NOT NULL, claimed_at TEXT NOT NULL,"
            "  PRIMARY KEY (task, bucket))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pyquant_signals ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  symbol TEXT NOT NULL, side TEXT NOT NULL, entry_price REAL NOT NULL,"
            "  sl_price REAL, tp_price REAL, qty REAL, entry_capital REAL,"
            "  expected_max_loss REAL, mode TEXT NOT NULL DEFAULT 'live',"
            "  status TEXT NOT NULL DEFAULT 'open', outcome_price REAL,"
            "  realized_pnl REAL, opened_at TEXT NOT NULL, closed_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pyquant_runs ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  run_type TEXT NOT NULL, symbol TEXT, params TEXT, metrics TEXT NOT NULL,"
            "  started_at TEXT, finished_at TEXT NOT NULL)"
        )
        # Kaggle backtest-integration addition — see the matching Postgres
        # comment above. SQLite's ADD COLUMN has no IF NOT EXISTS, so this
        # is tried once per connection and the "duplicate column" error is
        # swallowed on every call after the first.
        for _ddl in (
            "ALTER TABLE pyquant_runs ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'",
            "ALTER TABLE pyquant_runs ADD COLUMN kaggle_ref TEXT",
            "ALTER TABLE pyquant_runs ADD COLUMN error TEXT",
            "ALTER TABLE pyquant_signals ADD COLUMN realized_pnl_pct REAL",
        ):
            try:
                conn.execute(_ddl)
            except sqlite3.OperationalError:
                pass  # column already exists
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Generic key/value state (PositionGuard's monitored_orders, optimal_params) ──

def get_json(key: str, default: Optional[dict] = None) -> Any:
    """Reads a JSON-serializable value by key. Returns `default` if absent or on error."""
    try:
        if _using_postgres():
            with _pg_conn().cursor() as cur:
                cur.execute("SELECT value FROM pyquant_kv WHERE key = %s", (key,))
                row = cur.fetchone()
                return json.loads(row[0]) if row else default
        with _lock, _sqlite_conn() as conn:
            row = conn.execute("SELECT value FROM pyquant_kv WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else default
    except Exception:
        return default


def set_json(key: str, value: Any) -> None:
    """Upserts a JSON-serializable value by key."""
    payload = json.dumps(value, default=str)
    now = datetime.now(timezone.utc).isoformat()
    if _using_postgres():
        with _pg_conn().cursor() as cur:
            cur.execute(
                "INSERT INTO pyquant_kv (key, value, updated_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (key, payload),
            )
        return
    with _lock, _sqlite_conn() as conn:
        conn.execute(
            "INSERT INTO pyquant_kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, payload, now),
        )


# ── Run guards (idempotency for QStash's at-least-once delivery) ────────────

def try_claim_run(task: str, bucket: str) -> bool:
    """
    Atomically claims `bucket` (e.g. "2026-10-13T14:00" for an hourly task,
    "2026-10-13T14:37" for a minute task) for `task`. Returns True the
    FIRST time a given (task, bucket) pair is claimed, False on every
    subsequent attempt — including a QStash retry of the same delivery, or
    two invocations racing each other. Replaces
    has_function_run_this_hour()/has_function_run_this_minute()'s
    file-timestamp comparison, which only worked for a single process
    reading its own local disk.
    """
    try:
        if _using_postgres():
            with _pg_conn().cursor() as cur:
                cur.execute(
                    "INSERT INTO pyquant_run_guard (task, bucket, claimed_at) "
                    "VALUES (%s, %s, now()) ON CONFLICT (task, bucket) DO NOTHING "
                    "RETURNING task",
                    (task, bucket),
                )
                return cur.fetchone() is not None
        with _lock, _sqlite_conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO pyquant_run_guard (task, bucket, claimed_at) VALUES (?, ?, ?)",
                    (task, bucket, datetime.now(timezone.utc).isoformat()),
                )
                return True
            except sqlite3.IntegrityError:
                return False
    except Exception:
        # State store unreachable: fail OPEN (allow the run) rather than
        # silently never running the strategy — matches the old file-based
        # guard's behavior of "assume not run yet" on read error. Logged by
        # the caller, which has a logger; this module intentionally has none.
        return True


def current_hour_bucket(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H")


def current_minute_bucket(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M")


def current_15min_bucket(now: Optional[datetime] = None) -> str:
    """
    GitHub Actions cron migration: fifteen_minute_task() previously relied
    purely on p4_orchestra.py's schedule library calling it at exactly
    :00/:15/:30/:45 within one continuous process — it had no bucket guard
    of its own. A cron-triggered process that calls it unconditionally on
    every 5-minute tick needs this guard so it still only actually runs
    once per 15-minute window instead of 3x too often.
    """
    now = now or datetime.now(timezone.utc)
    floored_minute = (now.minute // 15) * 15
    return now.strftime("%Y-%m-%dT%H:") + f"{floored_minute:02d}"


def current_day_bucket(now: Optional[datetime] = None) -> str:
    """Same rationale as current_15min_bucket(), for daily_optimizer()."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


# ── Signals (Phase 2 — UI "Signals"/"Live Monitor" screens) ─────────────────
#
# One row per entry taken (or, in paper mode, simulated). Written by
# pyq_p4.py at signal-execution time (status="open") and updated when
# PositionGuard/check_open_position closes it (status="closed_tp" /
# "closed_sl" / "closed_manual", realized_pnl, outcome_price, closed_at).
# This is purely additive logging — nothing here feeds back into trading
# decisions, so it is safe to exercise with ordinary local tests without
# touching the live engine.

def log_signal(
    symbol: str,
    side: str,
    entry_price: float,
    sl_price: Optional[float] = None,
    tp_price: Optional[float] = None,
    qty: Optional[float] = None,
    entry_capital: Optional[float] = None,
    expected_max_loss: Optional[float] = None,
    mode: str = "live",
) -> Optional[int]:
    """Records a newly-opened signal/position. Returns its row id, or None on error."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        if _using_postgres():
            with _pg_conn().cursor() as cur:
                cur.execute(
                    "INSERT INTO pyquant_signals "
                    "(symbol, side, entry_price, sl_price, tp_price, qty, entry_capital, "
                    " expected_max_loss, mode, status, opened_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',now()) RETURNING id",
                    (symbol, side, entry_price, sl_price, tp_price, qty, entry_capital,
                     expected_max_loss, mode),
                )
                return cur.fetchone()[0]
        with _lock, _sqlite_conn() as conn:
            cur = conn.execute(
                "INSERT INTO pyquant_signals "
                "(symbol, side, entry_price, sl_price, tp_price, qty, entry_capital, "
                " expected_max_loss, mode, status, opened_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'open',?)",
                (symbol, side, entry_price, sl_price, tp_price, qty, entry_capital,
                 expected_max_loss, mode, now),
            )
            return cur.lastrowid
    except Exception:
        return None


def update_signal_outcome(
    signal_id: int,
    status: str,
    outcome_price: Optional[float] = None,
    realized_pnl: Optional[float] = None,
    realized_pnl_pct: Optional[float] = None,
) -> None:
    """Marks a signal closed (status e.g. "closed_tp"/"closed_sl"/"closed_manual").

    realized_pnl_pct (2026-10-25, PnL-by-percentage): a leveraged
    price-return fraction (pyquant_signal_math.outcome_pct()) — kept
    alongside the original dollar-based realized_pnl rather than
    replacing it, since realized_pnl is still meaningful in paper/live
    mode (a real wallet-balance delta) and some callers only have one of
    the two figures available."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        if _using_postgres():
            with _pg_conn().cursor() as cur:
                cur.execute(
                    "UPDATE pyquant_signals SET status=%s, outcome_price=%s, "
                    "realized_pnl=%s, realized_pnl_pct=%s, closed_at=now() WHERE id=%s",
                    (status, outcome_price, realized_pnl, realized_pnl_pct, signal_id),
                )
            return
        with _lock, _sqlite_conn() as conn:
            conn.execute(
                "UPDATE pyquant_signals SET status=?, outcome_price=?, "
                "realized_pnl=?, realized_pnl_pct=?, closed_at=? WHERE id=?",
                (status, outcome_price, realized_pnl, realized_pnl_pct, now, signal_id),
            )
    except Exception:
        pass  # logging is best-effort; never let it break the trading path


def list_signals(symbol: Optional[str] = None, status: Optional[str] = None, limit: int = 200) -> list[dict]:
    """Returns recent signals, newest first, optionally filtered by symbol/status."""
    where, params = [], []
    if symbol:
        where.append("symbol = %s" if _using_postgres() else "symbol = ?")
        params.append(symbol)
    if status:
        where.append("status = %s" if _using_postgres() else "status = ?")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    cols = ["id", "symbol", "side", "entry_price", "sl_price", "tp_price", "qty",
            "entry_capital", "expected_max_loss", "mode", "status", "outcome_price",
            "realized_pnl", "realized_pnl_pct", "opened_at", "closed_at"]
    try:
        if _using_postgres():
            with _pg_conn().cursor() as cur:
                cur.execute(
                    f"SELECT {','.join(cols)} FROM pyquant_signals{clause} "
                    f"ORDER BY id DESC LIMIT %s", (*params, limit),
                )
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        with _lock, _sqlite_conn() as conn:
            rows = conn.execute(
                f"SELECT {','.join(cols)} FROM pyquant_signals{clause} "
                f"ORDER BY id DESC LIMIT ?", (*params, limit),
            ).fetchall()
            return [dict(zip(cols, row)) for row in rows]
    except Exception:
        return []


# ── Runs (Phase 2 — UI "Backtest" screen: walkforward + daily-optimizer runs) ─

def record_run(
    run_type: str,
    metrics: dict,
    symbol: Optional[str] = None,
    params: Optional[dict] = None,
    started_at: Optional[str] = None,
) -> Optional[int]:
    """
    Records a completed walkforward/backtest/optimizer run. `run_type` is a
    free-form label (e.g. "walkforward", "daily_optimizer"). `metrics` is
    whatever compute_metrics()-style dict the caller has (win rate, total
    pnl, sharpe, max drawdown, trade count, etc.) — stored as JSON, read
    back parsed by the API layer.
    """
    now = datetime.now(timezone.utc).isoformat()
    metrics_json = json.dumps(metrics, default=str)
    params_json = json.dumps(params, default=str) if params is not None else None
    try:
        if _using_postgres():
            with _pg_conn().cursor() as cur:
                cur.execute(
                    "INSERT INTO pyquant_runs (run_type, symbol, params, metrics, started_at, finished_at) "
                    "VALUES (%s,%s,%s,%s,%s,now()) RETURNING id",
                    (run_type, symbol, params_json, metrics_json, started_at),
                )
                return cur.fetchone()[0]
        with _lock, _sqlite_conn() as conn:
            cur = conn.execute(
                "INSERT INTO pyquant_runs (run_type, symbol, params, metrics, started_at, finished_at) "
                "VALUES (?,?,?,?,?,?)",
                (run_type, symbol, params_json, metrics_json, started_at, now),
            )
            return cur.lastrowid
    except Exception:
        return None


_RUN_COLUMNS = ["id", "run_type", "symbol", "params", "metrics", "started_at", "finished_at", "status", "kaggle_ref", "error"]


def _row_to_run(row: tuple) -> dict:
    d = dict(zip(_RUN_COLUMNS, row))
    try:
        d["metrics"] = json.loads(d["metrics"]) if d["metrics"] else {}
    except Exception:
        pass
    try:
        d["params"] = json.loads(d["params"]) if d["params"] else None
    except Exception:
        pass
    return d


def list_runs(run_type: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Returns recent runs, newest first, optionally filtered by run_type."""
    clause, params = "", []
    if run_type:
        clause = " WHERE run_type = %s" if _using_postgres() else " WHERE run_type = ?"
        params.append(run_type)
    cols = ", ".join(_RUN_COLUMNS)
    try:
        if _using_postgres():
            with _pg_conn().cursor() as cur:
                cur.execute(
                    f"SELECT {cols} FROM pyquant_runs{clause} ORDER BY id DESC LIMIT %s", (*params, limit),
                )
                return [_row_to_run(row) for row in cur.fetchall()]
        with _lock, _sqlite_conn() as conn:
            rows = conn.execute(
                f"SELECT {cols} FROM pyquant_runs{clause} ORDER BY id DESC LIMIT ?", (*params, limit),
            ).fetchall()
            return [_row_to_run(row) for row in rows]
    except Exception:
        return []


# ── Kaggle backtest integration (Section 6.1 of CLAUDE.md) ──────────────────
#
# A UI-submitted backtest request doesn't run synchronously inside the
# Vercel API (see CLAUDE.md/KAGGLE.md for why — Vercel has neither the
# compute time nor a durable filesystem for this). Instead, the API calls
# Kaggle's API directly to launch a kernel with the chosen params, writes
# an immediate "queued" row here so the UI has something to show right
# away, and the Kaggle notebook itself calls update_run_status() (via a
# small HTTP call back to a new API endpoint, or directly against this
# same DATABASE_URL — see KAGGLE.md for which) as it moves from queued to
# running to completed/failed, with the real metrics attached at the end.

def request_backtest(run_type: str, params: dict, kaggle_ref: Optional[str] = None,
                      symbol: Optional[str] = None) -> Optional[int]:
    """Writes a new 'queued' row for a UI-submitted backtest/optimizer
    request. Returns the new row's id (the UI polls get_run(id) for
    status), or None on failure."""
    now = datetime.now(timezone.utc).isoformat()
    params_json = json.dumps(params, default=str) if params is not None else None
    try:
        if _using_postgres():
            with _pg_conn().cursor() as cur:
                cur.execute(
                    "INSERT INTO pyquant_runs (run_type, symbol, params, metrics, started_at, finished_at, status, kaggle_ref) "
                    "VALUES (%s,%s,%s,%s,now(),now(),'queued',%s) RETURNING id",
                    (run_type, symbol, params_json, "{}", kaggle_ref),
                )
                return cur.fetchone()[0]
        with _lock, _sqlite_conn() as conn:
            cur = conn.execute(
                "INSERT INTO pyquant_runs (run_type, symbol, params, metrics, started_at, finished_at, status, kaggle_ref) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_type, symbol, params_json, "{}", now, now, "queued", kaggle_ref),
            )
            return cur.lastrowid
    except Exception:
        # Best-effort, matching this module's other writers — logged by
        # the caller (the API layer), which has a logger; this module
        # intentionally has none.
        return None


def update_run_status(run_id: int, status: str, metrics: Optional[dict] = None,
                       kaggle_ref: Optional[str] = None, error: Optional[str] = None) -> bool:
    """Moves a queued/running row forward (status='running'|'completed'|
    'failed'). When metrics is given, also overwrites metrics and bumps
    finished_at to now() — call this on the terminal transition
    (completed/failed), not on the queued->running one. Best-effort: never
    raises, returns False on failure so a caller (the Kaggle notebook, or
    a status-check endpoint) can log and move on rather than crash."""
    try:
        sets, vals = ["status = " + ("%s" if _using_postgres() else "?")], [status]
        if metrics is not None:
            sets.append("metrics = " + ("%s" if _using_postgres() else "?"))
            vals.append(json.dumps(metrics, default=str))
            sets.append("finished_at = now()" if _using_postgres() else "finished_at = ?")
            if not _using_postgres():
                vals.append(datetime.now(timezone.utc).isoformat())
        if kaggle_ref is not None:
            sets.append("kaggle_ref = " + ("%s" if _using_postgres() else "?"))
            vals.append(kaggle_ref)
        if error is not None:
            sets.append("error = " + ("%s" if _using_postgres() else "?"))
            vals.append(error)
        vals.append(run_id)
        set_clause = ", ".join(sets)
        if _using_postgres():
            with _pg_conn().cursor() as cur:
                cur.execute(f"UPDATE pyquant_runs SET {set_clause} WHERE id = %s", vals)
        else:
            with _lock, _sqlite_conn() as conn:
                conn.execute(f"UPDATE pyquant_runs SET {set_clause} WHERE id = ?", vals)
        return True
    except Exception:
        # Best-effort — logged by the caller, same convention as above.
        return False


# ── Trading on/off switch (Phase 2 — UI's LIVE/PAPER/OFF control) ───────────
#
# Scope note: LIVE vs PAPER is which Binance credentials/host the process
# connects to (testnet.binancefuture.com vs mainnet) — that connection is
# built once at process start (see p4_utils.py's _create_binance_client()),
# so switching it still requires restarting the orchestra process with
# PYQUANT_MODE set accordingly; the procurement/integration guide covers
# that restart step. OFF, by contrast, is made a genuinely live control
# here: it does NOT touch PYQUANT_MODE/credentials, and does NOT stop
# PositionGuard from continuing to manage any ALREADY-OPEN position's
# bracket orders (that would be unsafe — an open position must keep its
# protective SL/TP). It only gates placing NEW entry orders, checked by
# pyq_p4.py's hourly_task() at the top of every cycle, so a toggle here
# takes effect on the next scheduled run without a restart.

_TRADING_ENABLED_KEY = "trading_enabled"


def is_trading_enabled() -> bool:
    """True (the default) unless the UI has explicitly set OFF."""
    val = get_json(_TRADING_ENABLED_KEY, default={"enabled": True})
    return bool(val.get("enabled", True)) if isinstance(val, dict) else True


def set_trading_enabled(enabled: bool) -> None:
    set_json(_TRADING_ENABLED_KEY, {"enabled": bool(enabled)})


# ── Signals baseline ("fresh start") ─────────────────────────────────────
#
# 2026-10-26: the owner asked for a way to have the Dashboard/aggregate
# stats "start fresh" after the TP/SL close-tracking fix (PLAN.md's
# 2026-10-25 entry) closed a backlog of old signals whose PnL numbers
# predate that fix — without deleting any history, since it's still
# needed for the Backtest-vs-Live comparison page (compute_live_performance()
# below) and the Signals page's full record. This is a soft marker, not
# a data reset: everything before `reset_at` simply stops counting toward
# "recent"/aggregate figures; it's still there if you query for it
# directly (e.g. the Signals page's full history, or a future "all time"
# toggle).

_SIGNALS_BASELINE_KEY = "pyquant_signals_baseline"


def get_signals_baseline() -> Optional[str]:
    """Returns the ISO timestamp signals/stats should be considered
    "fresh" from, or None if no baseline has ever been set (meaning:
    count everything, the original behavior)."""
    val = get_json(_SIGNALS_BASELINE_KEY)
    return val.get("reset_at") if isinstance(val, dict) else None


def set_signals_baseline(reset_at: Optional[str] = None) -> str:
    """Sets (or re-sets) the baseline to `reset_at`, or now() if omitted.
    Returns the timestamp actually stored."""
    ts = reset_at or datetime.now(timezone.utc).isoformat()
    set_json(_SIGNALS_BASELINE_KEY, {"reset_at": ts})
    return ts


def clear_signals_baseline() -> None:
    """Reverts to "count everything" (no baseline)."""
    set_json(_SIGNALS_BASELINE_KEY, {"reset_at": None})


def compute_live_performance(since: Optional[str] = None, limit: int = 500) -> dict:
    """Aggregate win-rate / avg leveraged-%-return / trade-count summary
    over closed signals, optionally restricted to those opened at or
    after `since` (an ISO timestamp — pass get_signals_baseline()'s
    value to respect the "fresh start" marker). Shared by GET
    /api/dashboard (recent figures) and GET /api/compare (the
    Backtest-vs-Live comparison page, PLAN.md's 2026-10-26 entry) so the
    two screens can't quietly compute this two different ways."""
    rows = list_signals(limit=limit)
    closed = [r for r in rows if r.get("status") not in (None, "open")]
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            closed = [
                r for r in closed
                if r.get("opened_at") and datetime.fromisoformat(r["opened_at"]) >= since_dt
            ]
        except Exception:
            pass  # a malformed baseline timestamp degrades to "count everything", not a crash

    pct_vals = [r["realized_pnl_pct"] for r in closed if r.get("realized_pnl_pct") is not None]
    wins = [r for r in closed if r.get("status") == "closed_tp"]
    losses = [r for r in closed if r.get("status") == "closed_sl"]
    return {
        "since": since,
        "trade_count": len(closed),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": (len(wins) / len(closed) * 100.0) if closed else None,
        "avg_pnl_pct": (sum(pct_vals) / len(pct_vals)) if pct_vals else None,
        "pct_sample_size": len(pct_vals),
    }


def get_run(run_id: int) -> Optional[dict]:
    """Returns a single run by id, or None if not found/on error."""
    cols = ", ".join(_RUN_COLUMNS)
    try:
        if _using_postgres():
            with _pg_conn().cursor() as cur:
                cur.execute(f"SELECT {cols} FROM pyquant_runs WHERE id = %s", (run_id,))
                row = cur.fetchone()
                return _row_to_run(row) if row else None
        with _lock, _sqlite_conn() as conn:
            row = conn.execute(f"SELECT {cols} FROM pyquant_runs WHERE id = ?", (run_id,)).fetchone()
            return _row_to_run(row) if row else None
    except Exception:
        return None
