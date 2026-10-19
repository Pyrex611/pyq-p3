# Supabase Setup Guide — Database for `pyquant_state.py`

This covers getting `DATABASE_URL` from a standalone Supabase project
(created directly on supabase.com, not via Vercel's Marketplace) into
both the orchestra process and the Vercel API. For the orchestra/VPS and
Vercel deployment steps themselves, see `VPS_SETUP.md` and
`VERCEL_DEPLOY.md` — this file covers only the Supabase-specific pieces.

## What you actually need from Supabase (and what you don't)

| Value you have | Used by this project? | Why |
|---|---|---|
| Project URL | No | Supabase's REST/Auth (PostgREST) API endpoint — this project never calls it |
| Publishable key | No | A client-side REST API key — same reason |
| Anon public key | No | Same — for Supabase's data API, which `pyquant_state.py` never touches |
| Service role key | **No — and don't put it anywhere** | Bypasses Row Level Security on Supabase's API — unneeded here, and a needless secret to have floating in an env file |
| **Pooled connection string (Transaction pooler, port 6543)** | **Yes — use this, not the Direct one** | The raw Postgres connection `pyquant_state.py`'s `psycopg2` backend speaks, routed through Supavisor over IPv4. See the IPv6 incident note below for why this is the one to use. |
| ~~Direct connection string~~ | **No — do not use, see below** | IPv6-only on current Supabase projects; breaks on any host without IPv6 egress |
| **Password** | **Yes** | Substituted into the pooled connection string's placeholder |

In short: `pyquant_state.py` talks to Postgres directly, not through
Supabase's own API layer — four of your six values aren't used by
anything in this codebase.

## ⚠️ IPv6 incident (2026-10-19) — read this before Step 1

This guide originally pointed `DATABASE_URL` at Supabase's **Direct**
connection string (`db.PROJECT_REF.supabase.co:5432`). That broke
every GitHub Actions cron tick the moment it was set: Supabase's Direct
connection now resolves **IPv6-only** on current projects (Supabase
moved off dedicated IPv4 for this endpoint), and GitHub Actions' hosted
runners have **no outbound IPv6 connectivity**. The live symptom, seen
in a real run:

```
connection to server at "db.<ref>.supabase.co" (2a05:d012:...), port 5432 failed: Network is unreachable
```

Every `pyquant_state.py` write failed (non-fatal — each call catches
its own exception) — but the consequence was worse than "state doesn't
persist": `try_claim_run()`'s except-block **fails open** (deliberately,
so a state-store outage can't silently stop the strategy from running
at all — see its docstring) — meaning when the DB is unreachable, every
task believes its bucket has never been claimed, **every single tick**.
The live log confirmed this directly: `daily_optimizer()` — meant to
run once a day — ran on a 5-minute GitHub Actions tick, downloading
fresh historical data and running a full 150-trial Bayesian search
every single invocation. Same story for `hourly_task()` and
`fifteen_minute_task()` running every 5 minutes instead of their real
cadence, and the UKF per-tick-retraining fix (`DOCS.md` Section 10)
being unable to do its job, since "no persisted state" was true on
every tick, not just the first.

**Fix: use the Pooled connection string (Transaction mode, port
`6543`) instead — it's IPv4-compatible.** This is a config-only change
(`DATABASE_URL`'s value, in both the GitHub Actions repo secret and the
Vercel project's env vars) — no code changes needed, and this guide's
original "skip pooling, direct is simpler" advice (previously Step 2)
is now actively wrong for any host without IPv6 and has been removed.

## Step 1 — Build `DATABASE_URL` from the Pooled connection string

1. Open your Supabase project → **Settings → Database → Connection
   pooling** (or **Connect** → **Connection string** → **Transaction
   pooler**, depending on your project's dashboard version).
2. Copy the **Transaction pooler** string. It looks like:
   ```
   postgresql://postgres.YOUR_PROJECT_REF:[YOUR-PASSWORD]@aws-0-REGION.pooler.supabase.com:6543/postgres
   ```
   (Note the username is `postgres.YOUR_PROJECT_REF`, not plain
   `postgres` — the pooler uses this to route to your project; don't
   "simplify" it back to `postgres`, that's a different, wrong value.)
3. Replace `[YOUR-PASSWORD]` with your actual password. If it contains
   `@`, `:`, `/`, `#`, `?` or `%`, URL-encode that character first
   (`@` → `%40`, `#` → `%23`, etc.) — an unencoded special character
   silently breaks the connection string's parsing.
4. Append `?sslmode=require` — Supabase requires SSL.

Result (example, not your real values):
```
postgresql://postgres.abcdefghijkl:MyP%40ss123@aws-0-us-east-1.pooler.supabase.com:6543/postgres?sslmode=require
```

Set this one string as `DATABASE_URL` in **both** the GitHub Actions
repo secret (`GITHUB_ACTIONS_SETUP.md`) and the Vercel project's
environment variables (`VERCEL_DEPLOY.md` Part C) — both the orchestra
(via `cron_runner.py`, one-shot per tick) and the API (Vercel, one-shot
per request) are now short-lived-invocation shapes, so the same
Transaction-mode pooled string suits both; there's no longer a reason
to use two different connection strings for the two deployments the
way the old Direct-vs-pooled split implied.

**If you're running `p4_orchestra.py` directly on a VPS instead**
(`VPS_SETUP.md`'s fallback path, a genuinely long-running process) —
the Transaction pooler string above still works fine for
`pyquant_state.py`'s usage pattern (simple autocommit queries, no
prepared statements or session-level features), so there's no need to
special-case that deployment path either. Use the same string
everywhere unless you have a specific reason not to.

## Step 2 — Tables create themselves (no SQL editor needed)

`pyquant_state.py`'s `_bootstrap_postgres()` runs `CREATE TABLE IF NOT
EXISTS` automatically, the first time *any* process calls any of its
functions (`get_json`, `set_json`, `try_claim_run`, `log_signal`,
`record_run`, etc.) with `DATABASE_URL` set. There is no migration script
to run and no reason to open Supabase's SQL Editor.

To confirm the tables exist without opening Supabase's Table Editor, run
this from anywhere `psycopg2` is installed (it's already in
`requirements.txt` — nothing new to install):

```python
import psycopg2
conn = psycopg2.connect("YOUR_DATABASE_URL_HERE")
cur = conn.cursor()
cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
print(cur.fetchall())
```

Run it once before anything else touches the database — expect an empty
list (or a connection error if the string is wrong, which is exactly the
fast-fail you want before restarting the orchestra). Run it again after
the orchestra's first cycle — expect to see `pyquant_kv`,
`pyquant_run_guard`, `pyquant_signals`, `pyquant_runs`.

## Step 3 — Row Level Security: nothing to do, here's why

Supabase's dashboard will likely flag these new tables as "Unrestricted"
the first time anyone looks at them in Table Editor — that warning is
about access through Supabase's PostgREST/Auth layer (the anon/
service_role keys from the table above), which this project never uses.
`psycopg2` connects directly as the `postgres` role, which bypasses RLS
by default. There is no policy to write, no toggle to flip, and no
dashboard visit required for this.

## Is QStash needed for any of this? No — not yet

QStash becomes relevant only if `p4_orchestra.py` itself moves onto
Vercel's serverless functions (which can't run an infinite
`schedule.run_pending()` loop). Nothing in this project's current setup
does that: the orchestra keeps running as the same long-lived process it
always has (on the VPS per `VPS_SETUP.md`), and Vercel is hosting only
the read-only API/UI. Revisit QStash specifically if/when the decision is
made to move the orchestra itself off a persistent VPS — not before.

## Order, summarized

1. Build `DATABASE_URL` (Step 1 above).
2. Get the code onto the orchestra's machine — `VPS_SETUP.md` Part F.
3. Set `.env` there and restart the orchestra — `VPS_SETUP.md` Parts H–I
   (this is what creates the tables).
4. Confirm the tables exist (Step 3's script above).
5. `vercel link` + env vars + `vercel --prod` — `VERCEL_DEPLOY.md`
   Parts B–D.
6. Verify — `VERCEL_DEPLOY.md` Part E.

Everything Supabase-specific (Steps 1–4) needs zero dashboard visits
under normal conditions — the one documented exception is moving to a
pooled connection string later, if connection exhaustion ever actually
shows up.
