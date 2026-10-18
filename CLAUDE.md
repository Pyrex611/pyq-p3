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

### Orchestra deployment: migrated from "needs a VPS" to GitHub Actions cron — LIVE
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

**This is now actually deployed and running** — the repo is public, the
secrets/variables are set, and `.github/workflows/orchestra.yml` is
firing every 5 minutes. **The push happened directly from the person's
own machine, not through Claude** — Claude's GitHub App authorization
for `Pyrex611/pyq-p3` remains blocked (see the standing note at the end
of this section), so the delivered code had to be applied and pushed by
hand. Confirmed by `git fetch origin` from this session: `origin/main`
now carries that push (`df3b617`, "Fixed github actions .env file
error" — the person iterated on the `.env`-file fix themselves across
several commits before landing on the version now live). **The
git-history purge described below was never actually applied to
`origin/main`** — it only ever happened in a local, disposable clone
during the session that proposed it, and the person's direct push went
public without it. This is a non-issue in practice (no real secrets
were ever committed, confirmed before the purge was even proposed; the
purge was about strategy/operational detail in `optimal_params.json`/
`active_trades.json`/the log files, not credentials) — it's now a
nice-to-have cleanup item, not a blocker, and this file previously
overstated it as done.

**Status of this migration:**
- Code changes (UKF/tracker persistence, run-guards): done and deployed.
- Repo is public, secrets + `PYQUANT_MODE` variable are set, workflow is
  running — confirmed via `git fetch` showing active commits on
  `origin/main` through mid-October.
- **Not yet done:** the walkforward comparison to validate the
  restart-safe UKF/tracker persistence before trusting
  `PYQUANT_MODE=paper`/`live` output from this path (see
  `GITHUB_ACTIONS_SETUP.md`'s "What changed and why") — this is why
  `SIGNAL_ONLY_MODE` (new, see below) exists: it lets the platform start
  producing real signal/log/track output immediately without waiting on
  that validation, since it never risks capital either way.
- `VPS_SETUP.md` remains in the repo as a fallback if the GitHub Actions
  path proves too unreliable or too limited.

### Reliability bug found and fixed: per-tick UKF retraining — built this session, NOT yet pushed
Investigated because the person asked to "proceed with the development
sprint... need the platform up and running, generating and storing
signals" — before building anything new, the live GitHub Actions run
history was checked via `gh api`, and it showed a high failure rate (6
of the last 9 ticks failed, each within ~50 seconds — not a timeout).
Root cause: `pyq_p4.py`'s module-level UKF setup always called the
expensive `create_ukf_models()` (one Alpaca history fetch + an
18-model, every-historical-bar training loop) **on every single
import** — meaning every 5-minute cron tick, not just the first one
ever. The UKF-persistence work from the earlier GitHub Actions migration
restored `.x`/`.P` from the database right after this, but only after
paying this full, unnecessary, network-dependent cost first — explaining
both the waste and the intermittent failures (a transient Alpaca hiccup
during this unconditional fetch kills the whole tick).

**Fix**: `p4_utils.py`'s `UKFModel` gained `make_ukf_shell(symbol)` — builds
the 3 per-symbol UKF objects with the right hyperparameters but no
Alpaca call and no training loop (their `.x`/`.P` get overwritten by the
existing restore step immediately anyway, so a shell is exactly
equivalent to a freshly-trained-then-restored filter, just cheaper and
with no network dependency). `pyq_p4.py` now checks for persisted UKF
state FIRST: if found, it uses six `make_ukf_shell()` calls instead of
the six `create_ukf_models()` calls; only a genuine first-ever run (no
persisted state yet) pays the real cost. `retrain_daily_ukfs()` (the
intentional once-daily full re-anchor) is unchanged — this fix only
touches the unconditional per-tick path. Full writeup: `DOCS.md`
Section 10.

**Status**: code written, `py_compile`-clean, **not yet pushed** — same
GitHub App blocker. This needs to land before the next few ticks to stop
wasting Alpaca calls/compute — **but see the next item below: this fix
alone does NOT currently resolve the live outage**, because a second,
unrelated bug is killing every tick earlier in the process than this one.

### Binance "restricted location" error — 100% cron failure rate, root-caused and fixed this session, NOT yet pushed
Reported directly by the person: GitHub Actions cron runs started failing
with Binance rejecting every request as coming from a restricted
location. Confirmed via `gh api` against the workflow's run history:
100% failure across the last 15 runs, each dying in a consistent ~60
seconds. Root cause: `p4_utils.py` builds the Binance client
**unconditionally at module import time**
(`binance_client = _create_binance_client()`), with a 5-retry × 5s-delay
loop that raises `ConnectionError` if every retry fails against Binance's
geo-compliance block. Because `cron_runner.py` does a plain top-level
`import pyq_p4` with no try/except, that one exception kills the
**entire process** on every tick — before any of `pyq_p4.py`'s own logic
(including `SIGNAL_ONLY_MODE`'s checks, and the UKF fix above) ever runs.
This is a separate bug from the UKF retraining issue above, earlier in
module load order — fixing one does not fix the other; both need to ship.

This is a real compliance geofencing block, not a transient network
issue or a credentials problem — Binance's compliance system is known to
reject requests from broad categories of cloud/datacenter IP ranges, not
just specific geographies, specifically to block exactly this kind of
automated-bot/cloud-runner traffic. It will not clear up on retry, and
is very likely not specific to GitHub Actions — see `BINANCE_GEOBLOCK.md`
for the full diagnosis and why this also puts most of the free-tier VPS
options in `VPS_SETUP.md` (Oracle, GCP/AWS free tier, DigitalOcean,
Hetzner) under the same suspicion.

**Fix**: `_create_binance_client()` now checks `SIGNAL_ONLY_MODE`
directly. With it on, an unreachable Binance no longer crashes the
process — it logs a clear diagnostic (detecting "restricted location"/
"eligibility" in the error) and returns a `Client` object built without
the validating `.ping()` (or `None` if even that fails), letting the
other call sites' *existing* try/except fallbacks (`check_open_position()`,
`get_equities()`, etc. already degrade gracefully) do their job instead
of the whole tick dying before it gets there. With `SIGNAL_ONLY_MODE`
off (real trading intended), the crash-loud behavior is unchanged —
silently continuing without a validated exchange connection while real
order placement is believed to be active would be dangerous.

**Recommendation (see `BINANCE_GEOBLOCK.md` for full reasoning)**: split
hosting by concern. Keep signal generation/storage/simulated TP-SL
(`SIGNAL_ONLY_MODE=true`) on GitHub Actions — it needs no working
Binance connection at all with this fix, and it's free. Run real order
placement (paper or live, `SIGNAL_ONLY_MODE=false`) from a host on a
residential/home IP instead of any datacenter VPS, since that's the one
class of IP this kind of geo-block generally doesn't catch.

**Status**: code written, `py_compile`-clean, **not yet pushed** — same
GitHub App blocker, delivered as a zip alongside the UKF fix above.

### Signal-only mode (`SIGNAL_ONLY_MODE`) — built this session, NOT yet pushed
Added to satisfy the explicit ask: "the platform needs to at least be
able to start generating, logging and tracking live signals (without
any trading attempts at all, not even Binance paper, just our simulated
check if TP/SL hit)." Full design/rationale in `DOCS.md` Section 9 —
short version: a new env-driven `SIGNAL_ONLY_MODE` flag, a new
`simulate_signal_entry()` function (zero exchange calls — no
`futures_create_order`, no `futures_change_leverage`, no
`futures_exchange_info`), and three call-site changes in
`hourly_task()`'s signal branches that take priority over `TEST_MODE`/
`trading_enabled`. `TEST_MODE=True` alone could not satisfy this ask —
its existing implementation suppresses `signal_executor()` entirely,
which is also where the Phase 2 `log_signal()` call lives, so it would
have silenced logging along with order placement.

**Status:** code written and `py_compile`-clean, **not yet pushed** —
blocked by the same GitHub App authorization issue below. Delivered as
a zip instead (just the changed files — `pyq_p4.py`, `DOCS.md`,
`GITHUB_ACTIONS_SETUP.md`, `.env.example`,
`.github/workflows/orchestra.yml`, this file — per the person's
standing zip-format instruction in Section 7). **To actually turn this
on**: apply the zip, push from a machine with real GitHub access (same
as the orchestra migration above), then add the `SIGNAL_ONLY_MODE`
repository Variable (`true`) per `GITHUB_ACTIONS_SETUP.md` Part C. Until
then, the live workflow keeps running exactly as it already does today
(code without this change is what's currently deployed) — this is purely
additive and off by default (`SIGNAL_ONLY_MODE` defaults to `false` if
the variable is never added).

**Standing blocker — GitHub push access:** Claude's GitHub App still
isn't authorized for `Pyrex611/pyq-p3` (confirmed again this session: a
`git fetch` succeeds — it's a public repo, reads don't need auth — but
`git push` still 403s with the same GitHub App message). Every future
code change from a Claude session will need to be delivered the same
way this one was (zip of changed files) until one of these is done:
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

**Is Vercel compute enough for this? No — confirmed, not a close call.**
Two independent, both-disqualifying reasons: (1) **time limit** —
`walkforward.py` is compute-heavy (UKF training + a 150-trial Optuna
search over 15+ days of data), which runs for minutes; even Vercel Pro's
longest configurable serverless duration (800s) is tight-to-insufficient
for this specific workload, and Hobby's is far shorter. (2) **no durable
filesystem** — it depends on local CSV files (`btc_back366.csv`/
`eth_back366.csv`, both gitignored) that need to exist going in; Vercel
functions get only an ephemeral `/tmp`, wiped between invocations, with
no mechanism to pre-populate it from a 50+ MB historical dataset on every
cold start. This isn't a plan-tier question — no Vercel tier fixes
either constraint for this specific workload.

**So: Kaggle, not Colab, and not the orchestra process either.** The
person already uses both Kaggle and Google Colab for this exact
workload "due to the computational requirements," which independently
confirms point (1) above. Between the two, **Kaggle is the one with a
public, programmatic trigger API** (`kaggle kernels push` / `kaggle
kernels status`, using just an account API token) — Colab's free/Pro
tiers have no equivalent; triggering a Colab notebook without a human in
the loop needs paid Colab Enterprise/Vertex AI, which isn't worth
adding for this. That makes Kaggle the natural fit for "press a button
in the UI, a backtest runs, the UI picks up the result" — Colab stays
exactly what it already is today: a notebook the person runs by hand
when they want to.

**Implemented, not yet pushed** (same GitHub-push blocker as everything
else from a Claude session — see Section 5's standing note). Final
design, routing through Kaggle instead of the originally-sketched
VPS-poller pattern:
1. **`walkforward.py` was NOT given a params argument** — a signature
   refactor touching a validated backtest engine's entry point was
   judged too risky for the benefit. Instead, a new
   `_SUPPRESS_AUTO_RECORD_RUN` module flag (default `False`, so manual
   `python3 walkforward.py` usage is completely unaffected) plus the
   standard "override module constants before calling the function"
   pattern — `walkforward.WALK_FORWARD_DAYS = ...` etc. — set by the
   Kaggle-side caller. Zero changes to the function's internals.
2. **`pyquant_state.py` gained a real run lifecycle**: `pyquant_runs`
   now has `status` (`queued`/`running`/`completed`/`failed`,
   defaulting to `completed` for pre-existing rows written by the old
   `record_run()` path, which is unchanged and still used by manual
   runs and `daily_optimizer()`), `kaggle_ref`, and `error` columns
   (added via `ADD COLUMN IF NOT EXISTS` / a try/except for SQLite, so
   this is safe to run against an existing database). New functions
   `request_backtest(...)` (creates the queued row) and
   `update_run_status(...)` (moves it forward) — unit-tested against a
   disposable SQLite file in this session (request → running →
   completed, plus backward-compat with the old `record_run()` path).
3. **`POST /api/backtests`** in `api/index.py` — accepts the form's
   params, writes the queued row, then pushes a new version of a
   **single, reused Kaggle kernel** (`kaggle_backtest/`, via the
   `kaggle` PyPI package's `KaggleApi().kernels_push()`) with this run's
   id + param overrides baked into a `params.json` written alongside the
   kernel's code at push time. Needs three new Vercel env vars:
   `KAGGLE_USERNAME`, `KAGGLE_KEY`, `KAGGLE_DATASET_SLUG` — a narrow
   credential (launch/read kernel runs only, never trading), consistent
   with Section 7's "API never holds exchange credentials" rule.
4. **Why one reused kernel slug, not a fresh one per run**: Kaggle
   attaches a secret (`DATABASE_URL`, needed so the kernel can write its
   result back) to a kernel, not to one version of it — so a fresh slug
   per run would need someone to open it in the Kaggle editor and
   re-attach the secret by hand every single time, defeating the point
   of a no-human-in-the-loop trigger. One kernel, set up once (`KAGGLE.md`
   Part D/F), gets a new version pushed per run instead. Known
   consequence: only one backtest usefully in flight at a time.
5. **`kaggle_backtest/run_backtest.py`** — the kernel's own code. Clones
   this public repo fresh (always current), installs
   `requirements.txt`, copies the attached Kaggle Dataset's CSVs into
   place as `btc_back366.csv`/`eth_back366.csv`, applies this run's
   module-constant overrides, calls `walkforward.run_walk_forward()`,
   and reports `running`/`completed`/`failed` back via
   `update_run_status()` — using `DATABASE_URL` from the Kaggle Secret,
   same database the orchestra and Vercel API already both write to.
6. **UI**: a form on `backtest.html` (days, Bayesian trials, leverage,
   Bayesian on/off, ATR-adaptive SL on/off) that `POST`s to
   `/api/backtests` and polls `/api/runs/{id}` every 15s until
   completed/failed.

Full setup instructions (Kaggle token, the one-time kernel + dataset +
secret setup, troubleshooting): **`KAGGLE.md`**.

**Confirmed while building this, worth restating**: the hourly/live
signal-generation loop does **not** need Kaggle — it was never the
heavy workload, and a real reliability bug found during this same pass
(every 5-minute cron tick was unnecessarily re-running the full,
expensive UKF historical-training step — see `DOCS.md` Section 10) is
now fixed, making that loop cheap and reliable on GitHub Actions'
standard runner on its own.

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

- **Zip deliveries contain only new/modified files, each with its full,
  unabridged current content — never a whole-repo re-zip.** Explicit
  standing feedback: "GOING FORWARD, THE ZIP FILE SHOULD ONLY CONTAIN
  THE FULL, UNABRIDGED CODE FOR UPDATED/MODIFIED FILES RATHER THAN
  PROVIDING THE ENTIRE REPO ALL OVER." This matters more now that the
  person applies these zips by hand and pushes from their own machine
  (see Section 5's GitHub push blocker) — a whole-repo zip makes that
  manual diff/merge step much harder than it needs to be.
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
- **`KAGGLE.md`** — backtest-from-UI integration (Section 6.1): why
  Kaggle and not Vercel/Colab/the orchestra, one-time Kaggle account/
  kernel/dataset/secret setup, testing, known limitations.
- **`BINANCE_GEOBLOCK.md`** — the Binance "restricted location" cron
  outage: root cause, the fix, and why real order placement likely needs
  a residential/home IP rather than any datacenter VPS going forward.
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
