# SPEC.md — Technical specification

This is the structured reference for *what the system is*, as it
currently stands — components, contracts, data model, configuration.
`CLAUDE.md`/`DOCS.md` explain the reasoning and history behind these
choices in prose; this file is the terse, current-state table you check
before changing an interface, to confirm what's actually guaranteed
today without re-reading the narrative. If this file and `CLAUDE.md`/
`DOCS.md` ever disagree, that's a bug in the docs — fix whichever one is
stale in the same change that caused the drift, and say so in `PLAN.md`.

## 1. Components

| Component | File(s) | Role | Holds exchange credentials? |
| --- | --- | --- | --- |
| Trading engine | `pyq_p4.py`, `p4_utils.py` | Signal generation, order placement, position/bracket management | Yes (Binance, Alpaca) |
| Scheduler (VPS path) | `p4_orchestra.py` | Always-on process, ticks the engine on a schedule | Yes (imports the engine) |
| Scheduler (GitHub Actions path, current) | `cron_runner.py` + `.github/workflows/orchestra.yml` | Stateless, one-shot: calls every task once, relies on each task's own run-guard | Yes (imports the engine) |
| Shared math | `pyquant_signal_math.py` | SL/TP/leverage/profit-target formulas, used by both the engine and the backtest | No |
| Validated backtest | `walkforward.py` | Simulated strategy validation against historical CSVs, no capital | No |
| State store | `pyquant_state.py` | SQLite (local) / Postgres (`DATABASE_URL` set) dual backend: run guards, PositionGuard state, signals, runs | No |
| Resilience | `pyquant_resilience.py` | Retry/backoff + duplicate-order guards for Binance Algo Order calls | No (wraps calls made by the engine) |
| API | `api/index.py` (FastAPI, deployed to Vercel) | Read layer over the state store + one off-switch write; `GET /api/heartbeat` (added 2026-10-20) surfaces `cron_runner.py`'s per-tick heartbeat and `pyq_p4.py`'s per-symbol signal diagnostics so the UI can distinguish "quiet but alive" from "not running" | **No, by design — see Section 4** |
| Frontend | `web/*.html` + `web/assets/` | Dashboard, Live Monitor, Signals, Backtest, Settings | No |
| Backtest-on-demand | `kaggle_backtest/run_backtest.py` + `kernel-metadata.json` | Runs `walkforward.py` on Kaggle, triggered by the API | No (only `DATABASE_URL`, via a Kaggle Secret) |

## 2. Execution tiers (promotion path for any strategy change)

| Tier | Trigger | Capital at risk | Exchange | Code path |
| --- | --- | --- | --- | --- |
| Backtest | `python3 walkforward.py` (manually or via Kaggle) | None | None | `walkforward.py` only |
| Signal-only | `SIGNAL_ONLY_MODE=true` | None | None (zero exchange calls) | `pyq_p4.py`'s `simulate_signal_entry()` |
| Paper | `PYQUANT_MODE=paper` | None (testnet) | Binance Futures **testnet** | `pyq_p4.py`'s `signal_executor()` against testnet credentials |
| Live | `PYQUANT_MODE=live` (default) | **Real** | Binance Futures mainnet | `pyq_p4.py`'s `signal_executor()` against mainnet credentials |

A change is only trusted at a given tier once it's passed every tier to
its left. `SIGNAL_ONLY_MODE` and `PYQUANT_MODE` are independent flags —
`SIGNAL_ONLY_MODE=true` overrides `PYQUANT_MODE`/`trading_enabled`
entirely regardless of their values (see `pyq_p4.py`'s
`SIGNAL_ONLY_MODE` comment block for the exact precedence).

## 3. Task schedule (GitHub Actions path, current)

| Task | Cadence | Guard | What it does |
| --- | --- | --- | --- |
| `one_minute_task()` | Effectively 5 min (GitHub's cron floor) | `try_claim_run()` on a 1-min bucket | Last-resort stop-loss check |
| `five_minute_task()` | 5 min (matches cron cadence 1:1) | None (runs every invocation) | — |
| `fifteen_minute_task()` | 15 min | `try_claim_run()` on a 15-min bucket | — |
| `hourly_task()` | 1 hr | `try_claim_run()` on an hour bucket | Signal generation (real or simulated), balance check, stale-order cleanup |
| `daily_optimizer()` | 1 day | `try_claim_run()` on a day bucket | Data refresh, Bayesian/grid search, UKF re-anchor |

Every task is called unconditionally, every 5-minute cron tick, by
`cron_runner.py`; each one deciding for itself whether it's actually due
is what makes that safe. **Any new task function added to this list
must self-guard the same way** (standing rule, `CLAUDE.md` Section 7).

## 4. Trust boundary

The API (`api/index.py`) **never** holds Binance/Alpaca credentials and
**never** calls either exchange directly. It is a read layer over
`pyquant_state.py` plus exactly one write: flipping `trading_enabled`.
That switch only suppresses *new* entry orders on the engine's next
cycle — it never touches an already-open position's protective
bracket orders, and it cannot place an order itself. This boundary is
deliberate and load-bearing: a bug or compromise in the public-facing
UI/API cannot place a trade. Do not add a second write path to the API
without re-reading `CLAUDE.md` Section 7's standing rule on this first.

## 5. Data model (`pyquant_state.py`)

| Table/key | Backend | Written by | Read by | Purpose |
| --- | --- | --- | --- | --- |
| `pyquant_kv` | SQLite/Postgres | Engine | Engine, API | Generic key-value (UKF filter state, tracker/flag globals, PositionGuard's monitored orders, `pyq_p4_cron_heartbeat`, `pyq_p4_signal_diagnostics` — see Section 1's API row) |
| `pyquant_run_guard` | SQLite/Postgres | Engine (`try_claim_run()`) | Engine | Per-task-per-bucket claim, prevents double-running a task within its own period |
| `pyquant_signals` | SQLite/Postgres | Engine (`log_signal()`, `update_signal_outcome()`) | API (`/api/signals`) | Every signal generated (real or simulated), with outcome tracking |
| `pyquant_runs` | SQLite/Postgres | Engine (`record_run()`), API (`request_backtest()`, `update_run_status()`), Kaggle kernel | API (`/api/runs`) | Backtest/optimizer run history, with a `queued → running → completed/failed` lifecycle (columns: `status`, `kaggle_ref`, `error`) |
| `trading_enabled` (a `pyquant_kv` key) | SQLite/Postgres | API (the one write) | Engine | Global on/off switch for new entry orders |

Every write from the trading engine side is best-effort (wrapped in
try/except) — a logging failure must never block a trading decision.
This is a standing rule (`CLAUDE.md` Section 7), not a per-call choice.

## 6. Configuration (environment variables)

See `.env.example` for the authoritative, current list — this table is
a map of *why* each group exists, cross-check against that file for
exact names.

| Group | Examples | Required where |
| --- | --- | --- |
| Alpaca (market data) | `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Engine (VPS/GitHub Actions) |
| Binance (execution) | `BINANCE_API_KEY`, `BINANCE_SECRET_KEY`, `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_SECRET_KEY` | Engine (VPS/GitHub Actions) |
| Mode flags | `PYQUANT_MODE` (`live`\|`paper`), `SIGNAL_ONLY_MODE` (`true`\|`false`) | Engine |
| Database | `DATABASE_URL` | Engine, API, Kaggle kernel (via a Kaggle Secret) |
| Kaggle | `KAGGLE_USERNAME`, `KAGGLE_KEY`, `KAGGLE_DATASET_SLUG` | API (Vercel) only |
| Telegram (alerts) | `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | Engine |

## 7. External dependencies and their failure modes

| Dependency | Used for | Known failure mode | Handling |
| --- | --- | --- | --- |
| Binance Futures API | Order placement, position/account reads | Geo-compliance block from cloud/datacenter IPs (see `BINANCE_GEOBLOCK.md`) — deterministic, not transient | Non-fatal under `SIGNAL_ONLY_MODE` (as of the round-2 fix); fatal (by design) otherwise |
| Alpaca Market Data API | OHLCV bars for UKF training/stepping | Transient outages, rate limits | Caught per-call; a failure during UKF shell construction is now avoided entirely when persisted state exists (see `DOCS.md` Section 10) |
| Supabase Postgres | Shared state store | (a) Falls back to local SQLite **silently** if `DATABASE_URL` is unset — confirm it's actually set before debugging "empty data." (b) `DATABASE_URL` must be the **Transaction pooler** string (port 6543, IPv4) — the Direct connection string is IPv6-only and breaks on any host without IPv6 egress (GitHub Actions among them); see `SUPABASE_SETUP.md`'s IPv6 incident note. (c) Connection pool exhaustion, cold-start latency. |
| Kaggle API | On-demand backtest trigger | Push failure (bad credentials, slug mismatch) | `POST /api/backtests` marks the run `"failed"` immediately with the real error if the push itself fails |
| GitHub Actions | Cron scheduling | 5-minute minimum granularity; requires a public repo for free minutes | Accepted constraint, not worked around |

## 8. Non-functional requirements

- **No silent failure of the kind that looks like success.** A crashed
  tick must show as a failed GitHub Actions run, not a green checkmark
  (enforced by `cron_runner.py`'s non-zero exit on any task exception).
- **No real order placement from a context that can't validate the
  exchange connection.** This is why `SIGNAL_ONLY_MODE` off keeps the
  original crash-loud behavior everywhere it was touched during the
  Binance fix — see `INTENT.md` Section 5.
- **State must survive a process restart between any two ticks** — a
  requirement introduced by the GitHub Actions migration and binding on
  any future hosting change too.
- **The API's trust boundary (Section 4) is non-negotiable** without an
  explicit, deliberate decision to change it (see `CLAUDE.md` Section
  6.3 for the one place this is being reconsidered, and why that's
  flagged as a real architectural change, not a routine one).

## 9. Known gaps (tracked, not yet closed)

- `last_resort_stop_loss_check()`'s close path doesn't log a Phase 2
  signal close-out (no in-memory signal reference there).
- Paper mode is wired but has never actually been run end-to-end.
- The GitHub Actions UKF-persistence/restart-safety path has not been
  validated via a walkforward comparison against the previous
  continuous-process behavior (this is why `SIGNAL_ONLY_MODE` exists as
  an interim step — see `INTENT.md`).

## 10. Keep this file current

Update this file in the same change that adds a component, changes a
table's schema, adds a new environment variable, or changes the trust
boundary in Section 4. If a change here would also change `INTENT.md`
(rare) or needs a `PLAN.md` entry (usual), update those in the same
pass — don't let the three documents drift out of sync with each other.
