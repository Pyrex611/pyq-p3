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

## 9. Signal-only mode (`SIGNAL_ONLY_MODE`) — generate/log/track with zero exchange calls

Added so the platform can start producing real, trackable output —
signals generated from live Alpaca data, logged to `pyquant_signals`,
and watched for simulated TP/SL hits against real market prices —
*before* the GitHub Actions UKF-persistence migration above has been
validated against walkforward, and without taking on any execution risk
while that validation is pending.

**Why `TEST_MODE=True` alone could not do this.** `TEST_MODE`'s
existing implementation (predates this change) only logs a
`"[TEST MODE] ... bypassed"` message in `hourly_task()`'s signal-
execution branches and does nothing else — it does not update
`btc_tracker`/`eth_tracker`, does not set `btc_ordered_signal`/
`eth_ordered_signal`, and critically does not call
`state_store.log_signal(...)`, because that call lives *inside*
`signal_executor()`, reached only after a real
`binance_client.futures_create_order(...)` call succeeds.
`TEST_MODE=True` means `signal_executor()` is never called at all —
so turning it on would stop order placement AND all logging/tracking
in one move, which is not what "generate, log, and track" asked for.

**What was actually added** (`pyq_p4.py`):
- `SIGNAL_ONLY_MODE` — a new env-driven flag (unlike the hardcoded
  `TEST_MODE`/`WATCHDOG_MODE`), read once at import:
  `os.environ.get("SIGNAL_ONLY_MODE", "false")...`. Settable via a
  GitHub Actions repository **Variable** (same mechanism as
  `PYQUANT_MODE`) with no code change or redeploy — see
  `GITHUB_ACTIONS_SETUP.md` Part C.
- `simulate_signal_entry(signal, trade_val)` — a new function, the
  signal-only counterpart to `signal_executor()`. It makes **no**
  exchange calls at all (no `futures_change_leverage()`, no
  `futures_exchange_info()` lot-size lookup, no
  `futures_create_order()`). It computes an unrounded approximate `qty`
  for display purposes only, calls `state_store.log_signal(...,
  mode="simulated")` so the row is visibly distinct from a real
  live/paper entry on the UI's Signals page, and returns the same
  `signal` dict back as the "ordered" signal — `generate_signal()`
  already populates every key `one_minute_task()`'s legacy tracker
  reads (`order_type`, `entry_price`, `order_side`, `tp_price`,
  `sl_price`, `current_price`, `current_bal`), so no reshaping is
  needed for the tracker to work unchanged.
- `hourly_task()`'s three signal-execution branches (double-signal,
  BTC-only, ETH-only) each gained a new `if SIGNAL_ONLY_MODE:` branch
  ahead of the existing `if not TEST_MODE and trading_enabled: / elif
  not trading_enabled: / else:` chain — **`SIGNAL_ONLY_MODE` takes
  priority over both `TEST_MODE` and the UI's `trading_enabled`
  switch**: if it's on, no order is ever placed regardless of either.
  The new branch calls `simulate_signal_entry()` instead of
  `signal_executor()`, skips `guard.start_guard_for_order()` entirely
  (PositionGuard watches real Binance order ids — there is never one in
  this mode), and updates `btc_tracker`/`eth_tracker` the same way the
  real path does otherwise.
- `one_minute_task()`'s dynamic last-resort stop-loss check (which
  calls `check_open_position()` — a real Binance read) is now also
  gated `if not TEST_MODE and not SIGNAL_ONLY_MODE:` — there's never a
  real position to check for a signal-only "trade," so this avoids a
  needless Binance call every tick.

**What this does *not* change:** the legacy per-minute tracker
(`bo_signal`/`eo_signal` fill + TP/SL detection in `one_minute_task()`)
runs exactly as it always has — real incoming market bars are compared
against the (simulated) entry/TP/SL prices, and a hit is logged via the
same `_log_signal_close()` path a real trade would use. This is
deliberate: it's the "just our simulated check if TP/SL hit" the user
asked for, reusing the already-trusted tracker code rather than writing
a parallel implementation. Two existing read-only Binance/account calls
(`get_equities()` when a tracker's `open_trade` flag is true, and
`check_open_position()` inside the PositionGuard virtual-safeguard loop,
which never finds a monitored order in this mode and so is a no-op) can
still fire — these are account/position *reads*, never order
placements, and are the same calls the tracker already made before this
mode existed.

**Relationship to `PYQUANT_MODE`:** independent axis. `PYQUANT_MODE`
only selects which Binance credentials/endpoint would be used *if* an
order were placed (mainnet vs. testnet) — `SIGNAL_ONLY_MODE=true` means
no order is placed either way, so `PYQUANT_MODE` can stay at whatever
value it will eventually run under without affecting this mode's
behavior now.

**Recommended rollout** (see `CLAUDE.md` Section 5 for current status):
start with `SIGNAL_ONLY_MODE=true` as soon as the GitHub Actions cron
migration is actually pushed and running, so the UI's Signals/Live
Monitor pages start showing real (simulated-mode) data immediately,
independent of the still-pending walkforward validation of the UKF
restart-persistence. Once that validation passes and `trading_enabled`
is deliberately turned on from the UI, flip `SIGNAL_ONLY_MODE` to
`false` to let the engine place real (paper, then live) orders.

## 10. Reliability fix: cheap UKF shells instead of per-tick retraining

Found by checking the live GitHub Actions run history (`gh api
repos/Pyrex611/pyq-p3/actions/workflows/.../runs`) before starting new
work, per the standing practice of checking what's actually deployed
before assuming it works: 6 of the last 9 scheduled ticks had failed,
each within ~50 seconds of the "Run one orchestra tick" step starting —
too fast to be the workflow's 10-minute timeout, consistent with a
transient failure early in the process.

**Root cause**: `pyq_p4.py`'s module-level UKF setup
(`ukf_model.create_ukf_models(...)`, called 6 times — 1H/15T/5T ×
BTC/ETH) runs unconditionally on every import. Each call does one
`crypto_client.get_crypto_bars()` Alpaca API call (fetching up to ~60
days of history) followed by `_train_ukf()`, which iterates every
single historical bar through `.predict()`/`.update()` for 3 sub-models
(close/high/low) — 18 full filter trainings total. On a continuously-
running process (the original VPS design) this happens once, at
startup. On a cron-triggered process restarting from scratch every 5
minutes (the GitHub Actions migration), it was happening on **every
single tick** — expensive, wasteful, and a real Alpaca-API/network
dependency on the hot path that the existing `_restore_ukf_state()` call
(which runs right after, overwriting `.x`/`.P` from the database) made
entirely pointless 5 out of every 6 ticks (everywhere except the very
first run, before any state exists to restore).

**Fix**: `p4_utils.py`'s `UKFModel` gained a new static method,
`make_ukf_shell(symbol)` — builds the same 3 UKF objects
(`_make_ukf(0.0, ...)` with each symbol's hyperparameters) but makes no
Alpaca call and runs no training loop. This is exactly equivalent to a
freshly-trained-then-restored filter for any tick where persisted state
already exists: `.Q`/`.R`/`.points`/`.fx`/`.hx` depend only on the
per-symbol hyperparameters (alpha/beta/kappa/P/Q/R), never on training
data — only `.x`/`.P` are data-dependent, and those get completely
overwritten by `_restore_ukf_state()` immediately after either path
anyway. `pyq_p4.py` now checks `state_store.get_json(_UKF_STATE_KEY)`
**before** building any models: if persisted state exists, it calls
`make_ukf_shell()` six times (no network call, no training loop) instead
of `create_ukf_models()`; only a genuine first-ever run (nothing
persisted yet) pays the real historical-fetch-and-train cost, exactly
once, which then seeds persisted state for every tick after it.

**Explicitly unchanged**: `retrain_daily_ukfs()` (the intentional,
once-daily full re-anchor on a trailing 30-day window, guarded by its
own day-bucket run guard) still does a real `get_crypto_bars()` fetch +
`build_ukfs()` training call — this fix only removes the *unconditional,
every-5-minutes* version of that cost, not the deliberate once-a-day one.

**Not yet validated end-to-end against a real GitHub Actions run** (the
same push-access blocker applies — see `CLAUDE.md` Section 5) — the fix
was verified by code inspection (confirming `_make_ukf()`'s hyperparameter-
only, data-independent construction) and `py_compile`, not by observing
an actual post-fix tick succeed. Worth specifically re-checking the
Actions run history after this ships.

## 11. Kaggle backtest integration

See `KAGGLE.md` for the full setup guide and `CLAUDE.md` Section 6.1 for
the design rationale (why Kaggle, why one reused kernel slug, why
`walkforward.py`'s module constants are overridden rather than the
function being given a params argument). Code summary:

- `pyquant_state.py`: `pyquant_runs` gained `status`/`kaggle_ref`/`error`
  columns (migrated via `ADD COLUMN IF NOT EXISTS` for Postgres, a
  try/except for SQLite — safe against an existing database) and two new
  functions, `request_backtest()` and `update_run_status()`, giving a run
  a real `queued → running → completed/failed` lifecycle instead of only
  ever being written once, complete, at the end (`record_run()`'s
  original behavior, kept unchanged and still used by manual
  `python3 walkforward.py` runs and `daily_optimizer()`).
- `walkforward.py`: new `_SUPPRESS_AUTO_RECORD_RUN` flag (default
  `False`) lets a caller that's tracking its own `pyquant_runs` row (the
  Kaggle path) skip the function's own automatic `record_run()` call at
  the end, so the UI's queued row gets updated in place instead of
  ending up with an orphaned queued row plus an unrelated duplicate
  completed one. No other change to this file.
- `api/index.py`: new `POST /api/backtests` — writes the queued row,
  then pushes a new version of a single, reused Kaggle kernel (via the
  `kaggle` PyPI package) with this run's id + param overrides baked into
  a `params.json`. Needs `KAGGLE_USERNAME`/`KAGGLE_KEY`/
  `KAGGLE_DATASET_SLUG` as new Vercel env vars.
- `kaggle_backtest/run_backtest.py` + `kernel-metadata.json`: the
  kernel's own code (clones the public repo, installs deps, copies the
  attached dataset's CSVs into place, applies overrides, calls
  `walkforward.run_walk_forward()`, reports status back) and its push
  template.
- `web/backtest.html`: a form (days, Bayesian trials, leverage, Bayesian
  on/off, ATR-adaptive SL on/off) that `POST`s to `/api/backtests` and
  polls `/api/runs/{id}` every 15s until the run completes or fails.

## 12. Binance "restricted location" cron outage: unconditional client construction at import time

See `BINANCE_GEOBLOCK.md` for the full diagnosis, the recommended
hosting split, and the architectural implication for other free-tier
hosts. Technical summary of the fix itself:

**The bug.** `p4_utils.py` has always built the Binance client at
module scope — `binance_client = _create_binance_client()` — rather than
lazily on first use. `_create_binance_client()` retries up to 5 times
(5s apart) and, if every attempt fails, `raise`s a `ConnectionError`.
That's fine when the failure is a real, transient outage worth crashing
loudly over — but Binance's compliance/geofencing block on GitHub
Actions' hosted-runner IPs is deterministic, not transient: every single
retry fails identically, so the function always ends up raising. Because
`cron_runner.py` does an unguarded top-level `import pyq_p4` (which
transitively imports `p4_utils` before any of `pyq_p4.py`'s own module-
level code, including the UKF setup covered in Section 10, runs at all),
that one raised exception aborts the whole Python process — meaning the
tick fails as a hard crash before `SIGNAL_ONLY_MODE`'s checks, before
signal generation, before logging, before anything.

Confirmed via `gh api repos/Pyrex611/pyq-p3/actions/workflows/<id>/runs`
against the actual run history: 100% failure across the most recent 15
runs, every one dying in ~60 seconds (5 retries × 5s delay ≈ 25-30s, plus
import/connection overhead on both sides) — consistent with a crash
during import, not a timeout partway through real work.

**The fix.** `_create_binance_client()` now reads `SIGNAL_ONLY_MODE`
directly (`os.environ.get("SIGNAL_ONLY_MODE", "false")...` — duplicated
from `pyq_p4.py`'s own check rather than imported, since `p4_utils.py`
doesn't otherwise depend on `pyq_p4.py` and shouldn't start now just for
this one flag). Behavior after all retries are exhausted:
- `SIGNAL_ONLY_MODE` true → logs a diagnostic that explicitly calls out
  a detected "restricted location"/"eligibility" error as a geo-block
  (vs. some other kind of failure), then attempts to still construct and
  return a `Client` object with no `.ping()` validation; falls back to
  returning `None` only if constructing the object itself raises.
  Either way, the process does **not** crash — `binance_client` ends up
  either an untested `Client` or `None`, and every other call site that
  actually uses it (`check_open_position()`, `get_equities()`, etc.)
  already wraps its own calls in try/except with a graceful fallback, so
  those degrade the same way they would during any other real Binance
  outage, rather than the crash happening one layer upstream of where
  that handling lives.
- `SIGNAL_ONLY_MODE` false (default) → unchanged, still raises
  `ConnectionError` and crashes the process loudly. This mode means real
  order placement is intended; silently proceeding with no validated
  exchange connection in that case is a correctness/safety issue, not
  just a reliability one, so the original fail-loud behavior is kept.

**Verification (round 1, superseded).** `py_compile`-clean, not verified
against a live run at the time. **It was pushed, and a live run then
found it incomplete** — see Section 13.

**What this does not fix.** This makes the signal-only pipeline survive
Binance being unreachable; it does not make Binance reachable. Real
order placement (paper or live) from GitHub Actions, or from most
free-tier cloud/VPS hosts, is still expected to hit the same geo-block —
see `BINANCE_GEOBLOCK.md` for the recommended split (signal-only on
GitHub Actions, real trading from a residential/home IP).

## 13. Binance geo-block, round 2: `PositionGuard._detect_hedge_mode()` crashed the process the same way

**How it was found.** Section 12's fix was pushed and ran live. The
resulting GitHub Actions log showed the process still crashing — just
one call site later: `PositionGuard.__init__()` (instantiated
unconditionally at `pyq_p4.py` import time, same timing problem as
Section 12) calls `self._detect_hedge_mode()`, which retried 5 times
against `self.binance.futures_get_position_mode()` — raising
`AttributeError` each time (`binance_client` was `None`, per round 1's
`SIGNAL_ONLY_MODE` fallback) — then raised its own `ConnectionError`,
uncaught, same fatal outcome as before.

**Root cause.** Round 1 only changed what `_create_binance_client()`
itself returns. It did not audit whether every *other* piece of code
that uses that return value could tolerate it being `None` or an
unvalidated/geo-blocked `Client` object instead of a working one.
`_detect_hedge_mode()` was the first such caller that both (a) runs
unconditionally at import time (before `cron_runner.py`'s own per-task
try/except can catch anything) and (b) has no fallback of its own for a
non-functional `self.binance`.

**The fix.** `_detect_hedge_mode()` now checks `SIGNAL_ONLY_MODE`
directly, before attempting anything — deliberately not "is
`self.binance` `None`", because a geo-blocked but non-`None` `Client`
object (the other branch round 1 can produce) fails every real call
just as fatally, so that would have been the wrong test. The right test
is "do we even need a correct answer": hedge/one-way mode only affects
the `positionSide` param on a *real* bracket order
(`_place_bracket_order()`/`_place_bracket_order_fallback()`), and no
order of any kind is ever placed in signal-only mode — so when
`SIGNAL_ONLY_MODE` is on, the function now skips detection entirely,
logs a diagnostic, and returns `False` (one-way, the common default;
never actually consulted in this mode). When it's off, the original
retry-then-raise behavior is unchanged — real trading still needs a
confirmed, correct hedge-mode value, and silently guessing would be a
correctness risk, not just a reliability one.

**Full call-site audit (done alongside this fix, not just a reactive
patch of the one that crashed).** Every `binance_client`/`self.binance`
reference in `p4_utils.py` and `pyq_p4.py` was checked against: does it
run unconditionally at import time, and if so, is it actually reached
under `SIGNAL_ONLY_MODE`?
- `PositionGuard`'s other Binance-calling methods (`check_all_orders_and_positions()`,
  `_check_and_place_bracket()`, `_place_bracket_order()`,
  `_place_bracket_order_fallback()`, `_check_bracket_fills()`,
  `_cancel_sibling_algo_order()`) — already wrapped in their own
  try/except, and `check_all_orders_and_positions()` no-ops immediately
  when `monitored_orders`/`monitored_positions`/`_bracket_orders` are
  all empty, which they always are in signal-only mode (nothing ever
  populates them, since `start_guard_for_order()` is skipped in favor of
  `simulate_signal_entry()`). Already safe.
- `check_open_position()`, `get_equities()` — already wrapped with a
  graceful zero/default fallback. Already safe.
- `signal_executor()`, `start_guard_for_order()` — never called at all
  under `SIGNAL_ONLY_MODE`. Not a risk.
- `hourly_task()`'s stale-open-order cancellation
  (`futures_get_open_orders()`/`futures_cancel_all_open_orders()`) — was
  already non-fatal, but ran and logged a spurious error every tick for
  no reason under `SIGNAL_ONLY_MODE`. Fixed opportunistically: now
  gated on `not TEST_MODE and not SIGNAL_ONLY_MODE`.
- `PositionGuard.__init__`'s `_detect_hedge_mode()` call — the one
  actually crashing the process. Fixed as described above.

Nothing else in either file touches a Binance client unconditionally at
import time.

**Verification.** `py_compile`-clean on both files. Additionally, an
isolated unit test (run in this sandbox, `python-binance` installed
specifically for this) exercised the new logic directly in 4 cases:
`SIGNAL_ONLY_MODE` on with `binance=None`; `SIGNAL_ONLY_MODE` on with a
client object that raises a geo-block error on every call (reconstructing
the exact live failure precisely, not just the `None` case); `SIGNAL_ONLY_MODE`
off with `binance=None` (confirms the crash-loud behavior is preserved);
`SIGNAL_ONLY_MODE` off with a working client (confirms the real
detection path is unaffected). All 4 passed. Not yet verified against an
actual live GitHub Actions run — same standing push-access blocker; this
is the second time a fix in this area needed a live run to surface what
code review alone missed, so specifically re-checking the Actions run
history after this ships is worth doing before considering this area closed.
