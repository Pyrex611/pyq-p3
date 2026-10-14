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
