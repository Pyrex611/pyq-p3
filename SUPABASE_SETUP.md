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
| **Direct connection string** | **Yes** | This is the raw Postgres connection `pyquant_state.py`'s `psycopg2` backend speaks — the only thing `DATABASE_URL` needs |
| **Password** | **Yes** | Substituted into the Direct connection string's placeholder |

In short: `pyquant_state.py` talks to Postgres directly, not through
Supabase's own API layer — four of your six values aren't used by
anything in this codebase.

## Step 1 — Build `DATABASE_URL` from the Direct connection string

Supabase's Direct connection string looks like:
```
postgresql://postgres:[YOUR-PASSWORD]@db.YOUR_PROJECT_REF.supabase.co:5432/postgres
```

1. Replace `[YOUR-PASSWORD]` with your actual password. If it contains
   `@`, `:`, `/`, `#`, `?` or `%`, URL-encode that character first
   (`@` → `%40`, `#` → `%23`, etc.) — an unencoded special character
   silently breaks the connection string's parsing.
2. Append `?sslmode=require` — Supabase requires SSL.

Result (example, not your real values):
```
postgresql://postgres:MyP%40ss123@db.abcdefghijkl.supabase.co:5432/postgres?sslmode=require
```

This one string is the entire integration — no SDK, no client library,
nothing else to configure. Set it as `DATABASE_URL` in both the
orchestra's `.env` (`VPS_SETUP.md` Part H) and the Vercel project's
environment variables (`VERCEL_DEPLOY.md` Part C).

## Step 2 — Direct connection vs. pooled connection (and the one real UI visit)

`pyquant_state.py` opens one persistent connection per process and
reuses it (its `_pg_pool` lazy singleton). That's exactly right for
`p4_orchestra.py` — one long-running process, one connection, for its
entire lifetime. The Direct connection string from Step 1 is correct
for it as-is.

Vercel's serverless functions are a different shape: many short-lived
invocations, each potentially opening its own connection, against
Supabase's free-tier cap of roughly 60 direct Postgres connections. A
burst of dashboard traffic could exhaust that. Supabase's answer is a
separate **pooled connection string** (PgBouncer, port `6543`,
"Transaction" mode) — and this is the one place this guide can't hand
you a software-free shortcut: the exact pooled hostname depends on your
project's AWS region, which isn't derivable from anything in your
"what you have" list without opening **Settings → Database** in the
Supabase dashboard once.

**Practical recommendation: skip this for now.** Use the same Direct
connection `DATABASE_URL` for both the orchestra and the Vercel API. A
personal/small-team read-only dashboard is extremely unlikely to open 60
concurrent connections. If you ever see "too many connections" errors in
the API's logs, that's the signal to make the one dashboard visit, copy
the pooled string from **Settings → Database → Connection pooling**, and
set it as the Vercel project's `DATABASE_URL` specifically (the orchestra
keeps using the Direct one).

## Step 3 — Tables create themselves (no SQL editor needed)

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

## Step 4 — Row Level Security: nothing to do, here's why

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
