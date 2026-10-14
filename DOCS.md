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

Both of the JSON-backed pieces still get a best-effort local file mirror
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

## 6. What's explicitly NOT done yet

- Paper mode is wired at the Binance-client level (testnet credentials +
  endpoint) but has not been run — per the "testing deferred to
  integration" decision, nothing gets deployed or executed, live or
  paper, until integration testing is the explicit goal.
- The Postgres backend in `pyquant_state.py` is written but untested
  against a real Postgres instance — it will be exercised for the first
  time during the Phase 1 DB provisioning step, not before.
- Concurrency hardening so far covers the Algo Order placement/
  cancellation path only. `signal_executor()`'s main entry order
  (`futures_create_order`) and `check_open_position()` are not yet
  wrapped — candidates for the next resilience pass.
