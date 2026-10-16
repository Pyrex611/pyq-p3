# PyQuant — Architecture & Process Notes

This documents the state of the system after the Phase 0 (unification) and
Phase 1 (Pre-UI stabilization) passes of the PyQuant Stabilization Sprint.
It is the companion to the Claude Docs plan ("PyQuant Stabilization Sprint
— Pre-UI Technical Plan") — that doc has the phase/objective/task
breakdown; this file documents what the *code* actually does, for anyone
reading the repo without that context.

## 1. The three execution modes — don't confuse them

PyQuant now has three genuinely different things that could be called
"testing," and they validate different layers:

| Mode | What it runs | What it validates | Capital at risk |
|---|---|---|---|
| **walkforward.py** | A standalone backtest script, run manually (e.g. in Colab) | The *strategy* — signal generation, SL/TP logic, parameter search — against historical data | None (simulated) |
| **Paper mode** (`PYQUANT_MODE=paper`) | The real `pyq_p4.py` / `p4_orchestra.py` live pipeline, pointed at Binance Futures **testnet** | The *execution path* — auth, order placement, Algo Order bracket orders, fills, PositionGuard — against a real (test) exchange | None (testnet funds) |
| **Live** (`PYQUANT_MODE=live`, the default) | The same pipeline, pointed at Binance Futures mainnet | Everything, for real | Yes |

`walkforward.py` is the project's trusted, validated reference
implementation for the *strategy*. It is not a stand-in for paper mode —
a change can be perfectly validated in walkforward and still fail in live
for execution reasons (a malformed API parameter, a rate limit, a
hedge-mode mismatch) that walkforward has no way to exercise, because it
never talks to an exchange. Paper mode is what catches those.

## 2. Unified signal/SL/TP/ATR math — `pyquant_signal_math.py`

As of Phase 0, the constants and formulas that previously lived
separately in `walkforward.py` and `p4_utils.py` — SL floor/delta,
leverage, the profit-target early-exit fractions, `compute_profit` — live
in one shared module that both import. **If you need to change one of
these values, change it in `pyquant_signal_math.py`, not in either
file that imports it** — that's the whole point of extracting it: a
change made in only one place can no longer silently desync live from the
validated walkforward baseline.

The standing process (see the plan doc's Phase 3): a *behavioral* change
to the strategy (e.g. UKF retrain cadence) is implemented and proven in
`walkforward.py` first, confirmed against the validated baseline, and
only then ported to live. This applies starting after the UI exists,
which is what makes that loop fast enough to be worth enforcing on every
change.

## 3. Externalized state — `pyquant_state.py`

Three things that used to live on local disk now live in a database
(SQLite by default, Postgres when `DATABASE_URL` is set):

- **Run guards.** `hourly_task()` / `one_minute_task()` used to compare a
  local timestamp file's mtime to decide "have I already run this hour/
  minute?" That doesn't survive a stateless serverless invocation, and
  it's not atomic against two invocations racing each other (which is
  exactly what Upstash QStash's at-least-once delivery can produce).
  `state_store.try_claim_run(task, bucket)` is an atomic claim: the first
  caller for a given (task, bucket) pair gets `True`, every other caller
  — including a QStash retry of the same delivery — gets `False`.
- **PositionGuard's monitored orders** (previously `active_trades.json`).
- **The daily optimizer's output** (previously `optimal_params.json`,
  written by `p4_orchestra.py`'s `daily_optimizer()`, read by
  `SignalGenerator.load_params()`).
- **(Added for the GitHub Actions cron migration — Section 8)** the 18
  UKF filters' internal state (`.x`/`.P`), `btc_tracker`/`eth_tracker`
  plus a handful of scalar flags (double-order prevention, which symbol
  has an open position, etc.), and `WatchdogTracker`'s trade log
  (previously `watchdog_trades.json`).

All of the JSON-backed pieces still get a best-effort local file mirror
for human inspection on a VPS, but the database is what's actually read
back — the file is not load-bearing anymore.

See the "Phase 1 — Database + Scheduler" step-by-step for how to
provision the Postgres database and point `DATABASE_URL` at it for the
actual Vercel deployment; nothing above requires that to be done yet —
the SQLite default means today's VPS/orchestra deployment works
unchanged.

## 4. Resilience — `pyquant_resilience.py`

The Binance Algo Order API calls that place and cancel bracket (TP/SL)
orders now retry transient failures (timeouts, connection resets,
Binance's own rate-limit/disconnect codes) with exponential backoff,
instead of giving up after one attempt and leaving a position with no
stop-loss. This was the single most commonly reported live-reliability
failure mode historically ("Binance API errors, positions not closing"),
so it's the first resilience fix landed.

The Algo Order endpoint has no client-supplied order-id to de-duplicate a
retry against, so `retry_algo_order()` checks for an already-placed order
of the same type before any retry (not before the first attempt) and
aborts rather than risking a duplicate bracket order.

## 5. Capital-base fixes (Phase 0, worth restating here)

`expected_max_loss` (the figure the last-resort stop-loss and, as of this
pass, the profit-target early exit are both calibrated against) is now
calculated against the capital **actually allocated** to a given trade —
the 50/50 split on a double signal, or the 80% allocation on a single
signal — rather than the full wallet balance at signal-generation time,
before that split was known. `check_open_position()`'s profit-target
auto-close had the same class of bug independently (comparing against
wallet balance with ad-hoc 0.2/0.15 fractions) and is now calibrated
against margin-at-risk with walkforward's validated 0.225/0.175
fractions, computed directly from the position's real exchange size and
entry price.

## 6. Phase 2 — UI build (backend + frontend)

Phase 2 adds a read layer over the state store plus a deployable static
frontend, without touching the live trading decision logic itself:

- **`pyquant_state.py`** gained two new tables: `pyquant_signals` (one row
  per entry taken — symbol, side, entry/SL/TP, qty, capital, mode, and its
  eventual close outcome/realized PnL) and `pyquant_runs` (one row per
  `walkforward.py` or daily-optimizer run, with its full metrics/params
  payload as JSON). Both are purely additive logging: `pyq_p4.py`,
  `p4_utils.py`, `walkforward.py` and `p4_orchestra.py` all call these
  functions in `try/except`-wrapped, best-effort fashion — a logging
  failure never affects a trading decision, matching the same
  fail-safe pattern `pyquant_state.py`'s run guards already used.
- **`pyquant_state.py`** also gained a genuinely live trading on/off
  switch (`is_trading_enabled()` / `set_trading_enabled()`), checked by
  `pyq_p4.py`'s `hourly_task()` at the top of every cycle. Turning it OFF
  from the UI skips placing NEW entry orders on the next scheduled run —
  it deliberately does NOT stop `PositionGuard` from continuing to manage
  any already-open position's bracket orders, and does NOT touch
  `PYQUANT_MODE` (live vs. paper is still a restart-time, credential-level
  choice — see `api/index.py`'s `/api/status` docstring for why that one
  is intentionally not hot-swappable from a public-facing API).
- **`api/index.py`** — a FastAPI app, deployed as a single Vercel Python
  app via Vercel's **framework preset** (`pyproject.toml`'s
  `[tool.vercel].entrypoint` — not the older per-file `/api` model; see
  `VERCEL_DEPLOY.md` for why that distinction matters and how it's wired).
  It is a **read-only layer over the state store** (plus the one OFF-switch
  write above) and never holds Binance credentials or calls the exchange
  directly — everything it serves is already being written by the trading
  engine itself via the Phase 2 logging above. Endpoints: `/api/health`,
  `/api/status` (GET/POST), `/api/config` (masked credential-presence
  check), `/api/signals`, `/api/signals/open`, `/api/runs`,
  `/api/runs/{id}`, `/api/dashboard`.
- **`web/`** — five static HTML/CSS/JS pages (Dashboard, Live Monitor,
  Signals, Backtest, API Keys & Settings), visually matching the approved
  Design-canvas mockup's color tokens and layout, but a genuinely separate,
  deployable production frontend — not an edit to the `.dc.html` mockup
  files, which remain a design reference only. Each page fetches from the
  API above; no build step, no framework.

All of the above was tested locally — `pyquant_state.py`'s new tables via
direct assertions against a disposable SQLite file, `api/index.py` via
FastAPI's `TestClient`, and the five frontend pages via a headless-browser
check against a locally-running copy of the real API serving real
(seeded) data — none of which touches the live trading engine, consistent
with this project's standing "testing deferred to integration" rule
(ordinary software testing of new non-trading infrastructure was always
in scope; only ad hoc terminal testing of `pyq_p4.py`/`p4_orchestra.py`
itself is deferred).

## 7. What's explicitly NOT done yet

- Paper mode is wired at the Binance-client level (testnet credentials +
  endpoint) but has not been run — per the "testing deferred to
  integration" decision, nothing gets deployed or executed, live or
  paper, until integration testing is the explicit goal.
- The Postgres backend in `pyquant_state.py` is written but untested
  against a real Postgres instance — it will be exercised for the first
  time during the Phase 1 DB provisioning step, not before.
- Deployment is documented separately, step by step: `SUPABASE_SETUP.md`
  (database), `GITHUB_ACTIONS_SETUP.md` (orchestra process — current
  path) or `VPS_SETUP.md` (orchestra process — fallback path, see
  Section 8), `VERCEL_DEPLOY.md` (API + frontend) — the Claude Docs plan
  doc intentionally no longer carries this granular content, per the
  project's standing instruction to keep setup/procurement steps in the
  repo's `.md` files.
- LIVE ↔ PAPER mode switching still requires an orchestra process restart
  with `PYQUANT_MODE` changed — the UI's mode display is informational for
  that axis; only the OFF switch is a live, no-restart control (see
  Section 6).
- `last_resort_stop_loss_check()`'s position close does not yet log a
  Phase 2 signal close-out (it has no in-memory signal reference, only
  tracker state) — the three paths that do (PositionGuard's virtual
  close, the legacy TP/SL tracker, and `check_open_position()`'s
  profit-target exit) cover the overwhelming majority of real closes; this
  one is a narrower fallback path and a candidate for a follow-up pass.

## 8. GitHub Actions cron migration (current orchestra deployment path)

The orchestra (`p4_orchestra.py`) moved from "needs a VPS" to "runs as a
GitHub Actions cron job" — `cron_runner.py` (new) is a stateless,
one-shot entrypoint that calls every task function once per invocation;
each already self-guards against running too often via
`pyquant_state.py`'s `try_claim_run()` (two new guards were added —
`fifteen_minute_task()` and `daily_optimizer()` had none before, since
they'd always relied on `p4_orchestra.py`'s own scheduler calling them
at the right wall-clock offset within one continuous process).

This is a real code change to the trading engine, not just a hosting
swap — a cron-restarted process has no memory of the previous
invocation, so three kinds of previously in-memory-only state now
round-trip through the database every tick: the 18 UKF filters'
`.x`/`.P`, `btc_tracker`/`eth_tracker` plus a handful of scalar flags,
and `WatchdogTracker`'s trade log. **Not yet validated against
walkforward** — per the project's own standing rule, don't trust
`PYQUANT_MODE=paper`/`live` output from this path until that comparison
confirms the restart-safe behavior tracks the original continuous-process
behavior. See `GITHUB_ACTIONS_SETUP.md` for the full trade-off writeup
(5-minute cron floor vs. the original 1-minute cadence; why this only
stays free with a public repo) and `CLAUDE.md` Section 6.3 for how this
relates to the larger, still-future Vercel+QStash re-architecture.
`VPS_SETUP.md` remains as a fallback path if this turns out to be too
unreliable or too limited in practice — nothing about this migration
makes switching back harder.
