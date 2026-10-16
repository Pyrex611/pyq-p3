# CLAUDE.md — PyQuant Project Memory

This file is the single entry point for picking up this project in a new
session, with a different model, or after a long gap. Read this first,
then follow its pointers into `DOCS.md` (architecture detail) and the
`*_SETUP.md` guides (procedure) as needed — don't re-derive any of this
from the code alone before reading here.

## 1. What this project is

PyQuant is a BTC/ETH crypto futures auto-trading system: Alpaca for
market data, Binance Futures for execution, a per-symbol Unscented
Kalman Filter (UKF) signal model, a Bayesian (Optuna, falling back to
grid search) daily parameter optimizer, and a PositionGuard
order-management layer that places bracket (TP/SL) orders and
retries transient Binance API failures. The owner (user) is both the
trader and the sole developer/operator — this is a single-user system,
not a multi-tenant product.

The work across the sessions this file summarizes has been: unify the
strategy math so the validated backtest and the live engine can't
silently drift apart (Phase 0), make the engine's state survive restarts
and run safely on a serverless/retry-prone scheduler (Phase 1), and give
it a real UI instead of SSH + log-tailing (Phase 2, in progress). See
Section 6 for what's next.

## 2. Architecture at a glance

```
┌──────────────────────────┐        ┌────────────────────────────┐
│   VPS (or equivalent)    │        │         Vercel             │
│                          │        │                            │
│  p4_orchestra.py         │  reads │  api/index.py (FastAPI)    │
│   - the ONLY process     │◄──────►│   - read-only over the     │
│     that holds Binance/  │ writes │     state store, plus the  │
│     Alpaca credentials   │        │     one OFF-switch write   │
│   - infinite loop,       │        │   - serves web/ (5 static  │
│     schedule-based ticks │        │     pages) from the SAME   │
│     (1 min / 5 min /     │        │     app via a StaticFiles  │
│     15 min / hourly /    │        │     mount                  │
│     daily)               │        └───────────────┬────────────┘
└────────────┬─────────────┘                        │
             │                                      │
             └───────────────┐        ┌─────────────┘
                             ▼        ▼
                    ┌──────────────────────┐
                    │   Supabase Postgres    │
                    │  (pyquant_state.py's   │
                    │   SQLite/Postgres      │
                    │   dual backend)        │
                    │  pyquant_kv            │
                    │  pyquant_run_guard     │
                    │  pyquant_signals       │
                    │  pyquant_runs          │
                    └──────────────────────┘
```

Two processes, one shared database, strict separation of trust: the
Vercel API is deliberately **never** given exchange credentials, so a
bug or compromise in the public-facing UI/API can't place a trade — it
can only read what the orchestra already wrote, plus flip the one
narrow `trading_enabled` switch (which only suppresses *new* entries on
the orchestra's next cycle; it never touches an already-open position's
protective orders).

## 3. Repo map

**Live stack (all current work targets these files):**
- `pyq_p4.py` — the trading engine: signal generation calls, order
  placement, the legacy per-minute TP/SL tracker, PositionGuard
  integration, Phase 2's logging/OFF-switch wiring.
- `p4_utils.py` — shared helpers: position sizing, SL/TP math via
  `pyquant_signal_math.py`, `check_open_position()`'s profit-target
  early exit (now also closes out the matching Phase 2 signal log row).
- `p4_orchestra.py` — the scheduler/entrypoint. Imports `pyq_p4`'s task
  functions and `schedule`s them; this is the file you actually run
  (`python3 p4_orchestra.py`) to start the whole system. Also owns
  `daily_optimizer()` (data refresh → Bayesian search → UKF re-anchor).
- `walkforward.py` — the standalone, validated backtest. Confirms a
  strategy change is sound against historical data before it's ever
  ported to `pyq_p4.py`/`p4_utils.py`. **Currently a no-argument script**
  (`run_walk_forward()` takes no params; config is module-level
  constants at the top of the file, and it reads two local CSV files,
  `btc_back366.csv`/`eth_back366.csv`) — see Section 6.1 for the plan to
  change that.
- `pyquant_signal_math.py` — Phase 0's unification module: SL floor/
  delta, leverage, profit-target fractions, `compute_profit`. **Change
  shared constants here, never independently in `walkforward.py` or
  `p4_utils.py`.**
- `pyquant_state.py` — the externalized state store (SQLite locally,
  Postgres via `DATABASE_URL`). Run guards, PositionGuard's monitored
  orders, the daily optimizer's output, Phase 2's `pyquant_signals`/
  `pyquant_runs` tables, and the `trading_enabled` switch. Every write
  from the trading engine is best-effort (wrapped in try/except) — a
  logging failure can never affect a trading decision.
- `pyquant_resilience.py` — retry/backoff wrapper for Binance Algo
  Order (bracket order) API calls.
- `data_download.py`, `p4_search.py` — called by `p4_orchestra.py`'s
  `daily_optimizer()` (historical data refresh, Bayesian/grid search).
- `cron_runner.py` — the GitHub Actions cron entrypoint (current
  orchestra deployment path, see Section 5/6.3). Stateless, one-shot:
  calls every task function once and exits, relying on each one's own
  `try_claim_run()` guard to no-op if it isn't actually due yet.
- `api/index.py` — Phase 2's FastAPI backend, deployed to Vercel.
- `web/` — Phase 2's five static frontend pages (Dashboard, Live
  Monitor, Signals, Backtest, Settings) + `assets/style.css`,
  `assets/app.js`.
- `pyproject.toml` — Vercel's Python **framework-preset** manifest
  (`[tool.vercel].entrypoint = "api.index:app"`) — this, not
  `requirements.txt`, is what Vercel reads for the API's dependencies.
- `vercel.json` — deliberately `{}`. Do not add a `"rewrites"` or
  `"functions"` key here without reading `VERCEL_DEPLOY.md`'s opening
  note first — that combination is exactly what caused the Phase 2
  deploy to 404 on every path.

**Legacy / previous-generation (not part of current work, don't assume
parity with the files above):** `pyq_p3.py`, `p3_backtest.py`,
`pyquant_orchestra.py`, `pyquant_utils.py`, and their `.log` files. These
predate the "p4" rewrite and are kept in the repo for reference only.

**Housekeeping worth doing eventually (not urgent):** `pyq_p3.log`
(~800 KB) and `pyq_p4.log` (~289 KB) are committed to git and will keep
growing — add `*.log` to `.gitignore` and `git rm --cached` them next
time you're touching `.gitignore` anyway. Not done as part of this pass
to avoid scope creep on an unrelated commit.

## 4. The three execution tiers (don't confuse them)

Full detail in `DOCS.md` Section 1. Summary: `walkforward.py` (strategy
validation, simulated, no capital) → paper mode, `PYQUANT_MODE=paper`
(the real `pyq_p4.py` pipeline against Binance **testnet** — validates
the execution path) → live, `PYQUANT_MODE=live` (real capital). A
strategy change proves itself in walkforward first, against the
validated baseline, before it's ported to live — this is the whole
reason `pyquant_signal_math.py` exists as a shared module.

## 5. Status — what's done, what's deployed, what's blocked

### Done (code-complete, see `DOCS.md` Sections 2–6 for full detail)
- **Phase 0** — unified signal/SL/TP/ATR math (`pyquant_signal_math.py`).
- **Phase 1** — externalized state (`pyquant_state.py`'s run guards,
  PositionGuard state, optimizer output) + Algo Order retry/backoff
  (`pyquant_resilience.py`) + capital-base fixes.
- **Phase 2** — Phase 1/2 logging tables + trading on/off switch;
  `api/index.py` FastAPI backend; `web/`'s five-page frontend. Tested
  locally only (direct SQLite assertions, FastAPI `TestClient`, a
  headless-browser pass across all 5 pages) — **not yet exercised
  against the live trading engine**, per the standing "testing deferred
  to integration" rule (see Section 7).

### Deployed and verified
- **Vercel**: `https://pyq-p3.vercel.app` — live, confirmed working.
  `/api/health` returns `{"ok":true}`, the UI renders. Deployed via the
  framework-preset fix in `VERCEL_DEPLOY.md` after an earlier 404 (wrong
  `vercel.json` rewrite destination + a `requirements.txt` dependency
  that tripped Vercel's framework auto-detection into an inconsistent
  state — full root-cause writeup in that file).
- **Database**: a standalone Supabase Postgres project, set up per
  `SUPABASE_SETUP.md`. (Confirm `DATABASE_URL` is actually set in the
  Vercel project's env vars if `/api/dashboard` ever shows empty data
  with no orchestra explanation — that endpoint silently falls back to
  an ephemeral local SQLite file without it.)

### Orchestra deployment: migrated from "needs a VPS" to GitHub Actions cron
Per explicit direction, the orchestra's hosting plan changed from the VPS
path to a GitHub Actions cron job (`cron_runner.py` + `.github/workflows/
orchestra.yml`), with the full Vercel+QStash re-architecture (Section 6.3)
deferred to later. This was **not** a drop-in hosting swap — it required
real code changes to the trading engine (detailed in `DOCS.md` Section 8):
externalizing the 18 UKF filters' internal state, the legacy tracker
dicts/flags, and `WatchdogTracker`'s trade log, all now round-tripping
through `pyquant_state.py` every tick instead of living only in one
continuous process's memory. Two missing run-guards
(`fifteen_minute_task()`, `daily_optimizer()`) were also added, since a
cron caller invokes every task unconditionally and relies entirely on
each one's own guard to self-limit.

**Status of this migration:**
- Code changes: done (see `DOCS.md` Section 8 for the full list).
- `GITHUB_ACTIONS_SETUP.md`: written — repo secrets, the `PYQUANT_MODE`
  variable, making the repo public (required for this to stay actually
  free at a 5-minute cadence — see that file's cost analysis), and the
  git-history purge of `optimal_params.json`/`active_trades.json`/the
  log files (strategy/operational detail, not credentials — confirmed
  no real secrets were ever committed) needed before going public.
- **Not yet done:** pushing any of this (blocked by the same GitHub App
  authorization issue in Section 5 below — now blocking more than just
  routine commits, since the history-purge force-push and the
  public-visibility change both depend on push access too), actually
  flipping the repo to public, adding the GitHub Secrets, and running a
  walkforward comparison to validate the restart-safe UKF/tracker
  persistence before trusting `PYQUANT_MODE=paper`/`live` output from
  this path (see `GITHUB_ACTIONS_SETUP.md`'s "What changed and why").
- `VPS_SETUP.md` (DigitalOcean primary path + a free-tier alternatives
  table, Oracle Cloud's Always Free tier standing out) remains in the
  repo as a fallback if the GitHub Actions path proves too unreliable or
  too limited.
- **Until this is actually pushed and running**, Phase 2's logging
  tables and OFF switch have nothing feeding them, and the UI will show
  empty data — this remains the top priority once GitHub push access is
  restored.
- **GitHub push** — local commits are ahead of `origin/main` and can't
  be pushed: Claude's GitHub App isn't authorized for
  `Pyrex611/pyq-p3`. Remedy is one of:
  - Install the Claude GitHub App:
    https://github.com/apps/claude/installations/select_target
  - Reconnect GitHub from claude.ai settings:
    https://claude.ai/customize/connectors?auth_start=github&auth_start_force=1

  This does **not** block Vercel deploys (`vercel --prod` deploys from
  local files, independent of git) — it only blocks `git push` itself
  and GitHub-triggered auto-deploys. Getting code onto the VPS currently
  goes through manual `scp` of a zip for the same reason (see
  `VPS_SETUP.md` Part F).
- **Paper mode** — wired (testnet credentials/endpoint) but never run.
- **`last_resort_stop_loss_check()`'s close path** doesn't log a Phase 2
  signal close-out (no in-memory signal reference there) — a narrower
  fallback path, not the common case; candidate follow-up.

## 6. What's next — Phase 3 (post-UI) plan

The original, detailed strategy/performance analysis (alpha model
concerns, backtest/live discrepancy audit, the full phased
recommendation list, the "Honest Assessment" profitability section) is
still maintained in the Claude Docs plan doc, **not** duplicated here —
open it for that level of detail:
`https://claude.ai/code/artifact/2a54f458-6ebe-423b-b4c8-e2deac599d7c`
(that doc was condensed in this pass to drop granular setup/deploy
steps, which now live in this repo's `.md` files instead — its
dev-plan-level content is unchanged).

This section adds the concrete, UI-completion-focused items that came
out of actually using the deployed UI:

### 6.1 Make the Backtest page actually run backtests (current top UI gap)

Right now `web/backtest.html` is read-only — it lists past
`walkforward.py`/`daily_optimizer()` runs via `/api/runs` and tells the
person to go run `python3 walkforward.py` by hand for a new one. Goal:
configure and launch a backtest from the UI itself.

**Why this can't be a direct Vercel endpoint:** `walkforward.py` is
compute-heavy (UKF training + a 150-trial Optuna search over 15+ days of
data) and depends on local CSV files (`btc_back366.csv`/`eth_back366.csv`,
both gitignored, living wherever the orchestra runs) — it doesn't fit
Vercel's serverless function time/memory limits or filesystem. It has to
run on the same machine as the orchestra (VPS or equivalent).

**Proposed design**, reusing the state-store job-queue pattern this
project already established in Phase 2:
1. **Refactor `walkforward.run_walk_forward()` to accept an optional
   params/overrides argument** (it's currently a zero-argument function
   reading module-level constants — `WALK_FORWARD_DAYS`,
   `UKF_TRAIN_DAYS`, `LEVERAGE`, `USE_BAYESIAN`, `BAYESIAN_TRIALS`, etc.)
   while keeping today's no-argument manual CLI usage working unchanged
   (overrides default to the existing module constants).
2. **New state-store table or a new `status` value on `pyquant_runs`** —
   a UI-submitted backtest request is written as a row with
   `status="queued"` and the chosen params as its JSON payload, instead
   of a completed run's metrics.
3. **New `POST /api/backtests` endpoint** in `api/index.py` — accepts
   the form's params, writes the queued row via a new
   `pyquant_state.py` function (e.g. `request_backtest(params)`),
   returns its id immediately (the actual run happens asynchronously).
4. **Orchestra-side poller** — `p4_orchestra.py` gains a new scheduled
   check (reusing its existing `schedule` library, e.g. every
   `five_minute_task` tick) that looks for `status="queued"` rows, runs
   `walkforward.run_walk_forward(params)` for each, and writes the
   result back via the existing `record_run()` (flipping status to
   `"completed"` or `"failed"` with the error).
5. **UI**: a form on `backtest.html` (symbol(s), date-range/day-count,
   leverage, Bayesian on/off + trial count) that `POST`s to
   `/api/backtests`, plus polling/refresh so a queued run's status
   visibly progresses to completed without a page reload.

This mirrors the existing Phase 2 architecture exactly (Vercel writes a
request, the orchestra — the only process with the compute and the data
files — picks it up and writes the result back) rather than introducing
a new mechanism.

### 6.2 Other UI-completion candidates (noted, not yet designed in detail)
- **Trigger `daily_optimizer()` on demand from the UI**, same queued-job
  pattern as 6.1.
- **Editable (non-secret) settings from the UI** — `/api/config` is
  currently a read-only masked presence-check. Anything that's safe to
  hot-swap without a restart is a candidate; `PYQUANT_MODE` explicitly
  is **not** (see `api/index.py`'s `/api/status` docstring for why
  live/paper stays a restart-time, credential-level choice).
- **Per-symbol pause** (finer-grained than the current global
  `trading_enabled` switch).

### 6.3 Bigger architecture option: eliminate persistent hosting entirely (Vercel + QStash)

Brainstormed in full in `VPS_SETUP.md`'s "free alternatives" section;
**the GitHub Actions cron migration (above, Section 5) was the first,
immediate step in this direction** — it already proved out the hard
part (externalizing the UKF filters' state and the tracker/flag globals
into `pyquant_state.py` so the trading engine survives a cold restart
between ticks). The deferred, later step: move off GitHub Actions'
5-minute cron floor onto Upstash QStash scheduling Vercel serverless
functions directly — one per task (`hourly_task`, `five_minute_task`,
`one_minute_task`, `daily_optimizer`), each its own HTTP-triggered
endpoint in `api/index.py` (or a sibling module) instead of a script
GitHub checks out and runs. QStash can schedule at finer granularity
than GitHub Actions and avoids the "needs a public repo to stay free"
trade-off entirely, at the cost of a real deployment-shape change
(these functions would need to call Alpaca/Binance directly from inside
the Vercel project, which currently deliberately holds no exchange
credentials — see Section 7's standing rule on this — so this also
means revisiting that trust boundary, not just where the code runs).
Tracked here as the next step after the GitHub Actions path is confirmed
stable; `VPS_SETUP.md` and the GitHub Actions path both remain usable
fallbacks if/when this is attempted.

## 7. Standing rules (apply these without being re-asked)

- **"Testing deferred to integration" means the live trading engine
  specifically** — don't ad hoc test `pyq_p4.py`/`p4_orchestra.py`
  against a real exchange outside an explicit integration-testing pass.
  It does **not** ban ordinary software testing of new, non-trading
  infrastructure (state-store unit assertions, `TestClient` API tests, a
  headless-browser UI pass) — all of that is in scope and expected.
- **Change shared math in `pyquant_signal_math.py` only** — never patch
  `walkforward.py` or `p4_utils.py` independently; that's the entire
  point of Phase 0's unification.
- **All state-store writes from the trading engine are best-effort** —
  wrap new logging calls in try/except, fail open, never let a logging
  failure touch a trading decision. Match the existing pattern in
  `pyquant_state.py`/`pyq_p4.py` rather than inventing a new one.
- **Granular setup/procurement instructions live in this repo's `.md`
  files** (`SUPABASE_SETUP.md`, `VPS_SETUP.md`, `VERCEL_DEPLOY.md`), not
  in the Claude Docs plan doc, which is kept to essential status +
  development-plan-level content only. If you write a new granular
  how-to, add it as a new `.md` file here (or extend an existing one)
  and link it from this file's Section 8, not the Claude Docs doc.
- **Setup/procurement instructions must be maximally granular** —
  include account/project creation, exact UI button names and field
  order, not just the steps that differ from "obvious" defaults. This
  was explicit, standing feedback after an earlier Vercel guide assumed
  too much.
- **The API (`api/index.py`) never holds exchange credentials and never
  calls Binance/Alpaca directly** — it is a read layer over the state
  store plus the one OFF-switch write. Keep it that way; route anything
  that needs to actually trade or run compute-heavy backtests through
  the orchestra process instead (see Section 6.1's design for why).
- **This repo is public** (as of the GitHub Actions cron migration,
  Section 5) — real credentials must only ever live in GitHub Secrets/
  Vercel env vars/the VPS's `.env`, never in code or in a committed file.
  Before adding any new file that logs or dumps runtime state (another
  `*_trades.json`-style mirror, say), add it to `.gitignore` in the same
  change, not as a follow-up.
- **Any new task function called from `cron_runner.py` must self-guard**
  with `state_store.try_claim_run()` on its own bucket (see
  `pyquant_state.py`'s `current_*_bucket()` helpers) — `cron_runner.py`
  calls every task unconditionally on every invocation and relies
  entirely on each task deciding for itself whether it's actually due.

## 8. Document map

- **`CLAUDE.md`** (this file) — start here in a new session.
- **`DOCS.md`** — architecture/code reference: the three execution
  tiers, the signal-math unification, externalized state, resilience,
  capital-base fixes, Phase 2's feature list, what's explicitly not
  done yet.
- **`SUPABASE_SETUP.md`** — database provisioning, `DATABASE_URL`,
  direct vs. pooled connections, confirming tables exist.
- **`GITHUB_ACTIONS_SETUP.md`** — orchestra hosting, current path: repo
  secrets, the `PYQUANT_MODE` variable, making the repo public, the
  git-history purge needed first, verifying the workflow runs.
- **`VPS_SETUP.md`** — orchestra hosting, fallback path: DigitalOcean
  step-by-step, the free/free-tier alternatives brainstorm, systemd
  service setup.
- **`VERCEL_DEPLOY.md`** — API+frontend deploy: CLI/dashboard setup,
  env vars, the 404 root-cause writeup, troubleshooting.
- **Claude Docs plan doc** (`https://claude.ai/code/artifact/2a54f458-6ebe-423b-b4c8-e2deac599d7c`)
  — the original strategy/performance deep-dive and the phased
  development plan at dev-plan level; this file (`CLAUDE.md`) is now the
  more current source for "what's done / what's next," since that doc
  was condensed to drop granular setup content.
- **`.env.example`** — the full list of environment variable names the
  code actually reads (cross-check against both the VPS's `.env` and
  Vercel's project env vars).

## 9. Keep this file current

The person maintaining this project asked explicitly: **update this file
as development progresses**, not just at major phase boundaries. When you
finish a change that affects Section 5's status, Section 6's plan, or
adds/removes a standing rule in Section 7, update this file in the same
turn — don't let it drift back out of sync with reality. A future
session (or a different model) relies on this file being accurate more
than it relies on any other single thing in this repo.

## 10. Resuming in a new session — quick checklist

1. Read this file fully before touching code.
2. Check `git log --oneline -5` and `git status` — confirm what's
   actually committed vs. still local-only (the GitHub push blocker in
   Section 5 means local and `origin/main` can genuinely diverge).
3. Check Section 5 for current deployment status before assuming
   anything is live — "code complete" and "deployed" are tracked
   separately throughout this file on purpose.
4. Pick up the top unchecked item in Section 5 ("Not yet done") before
   starting new Phase 3 feature work, unless the person explicitly
   redirects — the VPS deployment is the current blocker for the
   already-built UI actually showing real data.
