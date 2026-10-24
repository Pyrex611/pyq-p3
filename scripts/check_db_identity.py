"""
check_db_identity.py — proves or disproves "GitHub Actions and Vercel are
writing to / reading from the same Postgres database" without running any
part of the actual trading engine.

Why this exists (2026-10-24): the engine side (GitHub Actions cron) was
clearly producing real signals — a live run on 2026-10-24 13:30 UTC logged
"BTC/USD signal: {...}" and "[SIGNAL-ONLY] BTCUSDT SELL Limit @ ... —
simulated entry, no exchange call made." GET /api/health (Vercel) also
reported its own Postgres connection as healthy at the same time. Yet
GET /api/heartbeat (also Vercel) showed no cron tick and no signal
diagnostics at all, even though cron_runner.py/pyq_p4.py's heartbeat code
had already been live for several days. Two healthy connections that
never see each other's writes is exactly what you'd see if the GitHub
Actions DATABASE_URL secret and Vercel's DATABASE_URL environment
variable point at two DIFFERENT databases (different Supabase project,
or the same project but a stale/old credential in one of the two places)
— each one individually "works," so neither side's own health check
would ever catch this; only a cross-check does.

This script is deliberately NOT part of the trading engine's own
dependency chain (no alpaca_trade_api/filterpy/python-binance/etc.) and
makes NO exchange calls — it only needs `psycopg2-binary`, which is
almost certainly already on your machine if you've run anything else in
this repo, and otherwise: `pip install psycopg2-binary`.

HOW TO RUN (locally, on your own machine — this does not belong in
GitHub Actions or Vercel, it's a one-off manual check):

    python3 scripts/check_db_identity.py "<GH_ACTIONS_DATABASE_URL>" "<VERCEL_DATABASE_URL>"

Paste the exact two connection strings (copy them straight out of the
GitHub repo's Settings -> Secrets and the Vercel project's Settings ->
Environment Variables — don't retype them by hand, a single dropped
character is a classic way for "the same" string to actually differ).

If you only want to check ONE string against itself (e.g. "does this
string even connect"), pass it twice.

WHAT IT DOES
1. Connects to both. If either connection fails outright, that's
   printed immediately and clearly — this alone may already be your
   answer (e.g. one of the two strings is simply wrong/stale).
2. Prints each connection's `current_database()`, `inet_server_addr()`,
   and `inet_server_port()` — the ground-truth proof of which physical
   database/host each string actually reached, independent of anything
   this project's own code does.
3. Writes a uniquely-timestamped marker row into `pyquant_kv` through
   connection A, then immediately tries to read that exact key back
   through connection B. If B sees it, they are DEFINITELY the same
   database (this is the strongest possible proof — stronger than
   comparing host/port strings, which can differ textually yet still
   resolve to the same database via a pooler). If B does NOT see it,
   they are DEFINITELY different databases, no further guessing needed.

This never touches `pyquant_run_guard`, `pyquant_signals`, or
`pyquant_runs` — only a disposable key in `pyquant_kv` named
`__db_identity_check__`, which is safe to leave behind (the real app
never reads that key) or delete afterward.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone

try:
    import psycopg2
except ImportError:
    print("psycopg2 isn't installed. Run: pip install psycopg2-binary")
    sys.exit(1)


def describe(label: str, dsn: str):
    print(f"\n--- {label} ---")
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
    except Exception as e:
        print(f"  CONNECTION FAILED: {type(e).__name__}: {e}")
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), inet_server_addr(), inet_server_port()")
        db, addr, port = cur.fetchone()
        print(f"  current_database() = {db}")
        print(f"  inet_server_addr()  = {addr}")
        print(f"  inet_server_port()  = {port}")
    return conn


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    dsn_a, dsn_b = sys.argv[1], sys.argv[2]

    conn_a = describe("Connection A (paste GitHub Actions' DATABASE_URL here)", dsn_a)
    conn_b = describe("Connection B (paste Vercel's DATABASE_URL here)", dsn_b)

    if conn_a is None or conn_b is None:
        print("\nAt least one connection failed outright — fix that string first; "
              "the identity check below can't run without both connecting.")
        sys.exit(1)

    marker_key = "__db_identity_check__"
    marker_value = f"{datetime.now(timezone.utc).isoformat()}|{uuid.uuid4().hex}"
    print(f"\nWriting marker through Connection A: {marker_value}")
    with conn_a.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS pyquant_kv ("
            "  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.execute(
            "INSERT INTO pyquant_kv (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (marker_key, marker_value),
        )

    print("Reading it back through Connection B...")
    with conn_b.cursor() as cur:
        cur.execute("SELECT value FROM pyquant_kv WHERE key = %s", (marker_key,))
        row = cur.fetchone()

    print()
    if row and row[0] == marker_value:
        print("RESULT: SAME DATABASE. Connection B saw the marker Connection A just wrote.")
        print("If the UI still shows no heartbeat, the problem is NOT a DATABASE_URL")
        print("mismatch — look instead at whether cron_runner.py's heartbeat code is")
        print("actually running/erroring on the GitHub Actions side (check the Action's")
        print("full log output, not just the summary), or at a stale Vercel deployment.")
    else:
        print("RESULT: DIFFERENT DATABASES (or different schemas/search_path).")
        print("Connection B did NOT see what Connection A just wrote.")
        print("Fix: make the GitHub Actions secret and the Vercel environment variable")
        print("literally the same string, then redeploy/re-run both sides and recheck")
        print("GET /api/heartbeat.")


if __name__ == "__main__":
    main()
