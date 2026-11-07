# CLAUDE.md — PyQuant Project Memory

Entry point for picking up this project in a new session. Read this
first, then follow pointers into `INTENT.md` (why)/`SPEC.md` (what)/
`PLAN.md` (status/roadmap) and the `*_SETUP.md`/incident `.md` files
(procedure/detail) as needed — this file stays short on purpose; detail
lives in those files, not duplicated here. Full document map: Section 8.

## 1. What this project is

PyQuant is a BTC/ETH crypto futures auto-trading system: Alpaca market
data, Binance Futures execution, a per-symbol Unscented Kalman Filter
(UKF) signal model, a Bayesian (Optuna, falling back to grid search)
daily optimizer, and a PositionGuard order-management layer (bracket
TP/SL, Binance retry/backoff). Single owner, trader + sole developer —
not multi-tenant. Full rationale: `INTENT.md`.

Work so far: unify strategy math so backtest and live engine can't drift
(Phase 0) → externalize state for a restart-safe/serverless scheduler
(Phase 1) → real UI instead of SSH + log-tailing (Phase 2) → UI
completion + reliability hardening (Phase 3, current). See `PLAN.md` for
granular status.

## 2. Architecture at a glance

```
┌──────────────────────────┐        ┌────────────────────────────┐
│  Orchestra (GH Actions    │  reads │  api/index.py (FastAPI,    │
│  cron, or VPS fallback)   │◄──────►│  Vercel) — read-only over  │
│   - ONLY holder of        │ writes │  the state store + ONE     │
│     Binance/Alpaca creds  │        │  off-switch write; serves  │
│   - one-shot per 5-min    │        │  Next.js frontend served   │
│     tick (cron_runner.py) │        └───────────────┬────────────┘
└────────────┬─────────────┘                        │
             └───────────────┐        ┌─────────────┘
                             ▼        ▼
                 Supabase Postgres (pyquant_state.py's SQLite/
                 Postgres dual backend) — pyquant_kv, _run_guard,
                 _signals, _runs
```

Trust boundary (non-negotiable, see `SPEC.md` Section 4): the API never
holds exchange credentials and can only flip `trading_enabled` — a bug
or compromise there cannot place a trade.

## 3. Repo map

**Live stack:**
- `pyq_p4.py` — trading engine: signals, order placement, PositionGuard
  wiring, `SIGNAL_ONLY_MODE`/logging.
- `p4_utils.py` — shared helpers: position sizing, SL/TP math, UKF
  factory (`create_ukf_models()`/`make_ukf_shell()`), Binance client
  construction, `PositionGuard`.
- `p4_orchestra.py` — scheduler/entrypoint (`python3 p4_orchestra.py`
  runs it standalone); also owns `daily_optimizer()`.
- `p4_search.py` — **found 2026-11-03, not previously documented here.**
  `daily_optimizer()` calls `run_grid_search()` from THIS file, not from
  `walkforward.py` — it's a complete independent duplicate of the UKF
  build, cost model, and SL/TP math, plus its own grid/Bayesian search.
  Its SL/TP constants currently match `pyquant_signal_math.py` exactly
  (verified by hand), so this isn't an active bug, but it's a standing-
  rule violation ("shared math only in `pyquant_signal_math.py`") and a
  latent drift risk: a future shared-math edit will silently NOT reach
  live's actual optimizer. Not fixed yet — flagging so it isn't
  rediscovered from scratch. See Section 5's 2026-11-03 entry.
- `walkforward.py` — the validated backtest, no-argument script (config
  = module constants, reads `btc_back366.csv`/`eth_back366.csv`).
- `pyquant_signal_math.py` — Phase 0's shared SL/TP/leverage/
  profit-target math. **Change constants here only**, never in
  `walkforward.py`/`p4_utils.py` independently.
- `pyquant_state.py` — externalized state (SQLite/Postgres). Run guards,
  PositionGuard state, signals/runs tables, `trading_enabled`. Every
  write from the engine is best-effort (try/except) by standing rule.
- `pyquant_resilience.py` — Binance Algo Order retry/backoff.
- `cron_runner.py` — GitHub Actions cron entrypoint: stateless,
  one-shot, calls every task unconditionally and relies on each task's
  own `try_claim_run()` guard.
- `api/index.py` — FastAPI backend, deployed as a standalone Python
  serverless function under `/api/*` (not the whole app anymore — see
  its own header comment for the 2026-10-29 Next.js migration).
  `pyproject.toml`'s `dependencies` array is what Vercel actually reads
  for deps — **not** `api/requirements.txt` (see `SPEC.md` Section 7 and
  the `kaggle` dependency incident, 2026-10-19, for why this matters: a
  dependency listed only in `requirements.txt` is silently never
  installed). No `[tool.vercel]` entrypoint anymore — that was specific
  to the old "route everything to this app" model; removing it was
  deliberate (see that file's own comment).
- `pages/`, `components/`, `lib/`, `styles/`, `package.json`,
  `next.config.js`, `jsconfig.json` — **the Next.js frontend
  (2026-10-29), replacing the old static `web/` site.** 6 pages
  (Dashboard `pages/index.js`, Live Monitor, Signals, Backtest, Compare,
  Settings), one shared `components/Layout.jsx` (topbar + nav — the
  single-source-of-truth fix for the "nav only showed Compare on some
  pages" class of bug the old copy-pasted-per-file `<nav>` produced),
  `lib/api.js` (API/formatting helpers, ported from the old
  `web/assets/app.js`). Same features/functionality/UI as the static
  site it replaced — this was a framework migration, not a redesign.
  See `PLAN.md`'s 2026-10-29 entry for the full writeup and what's
  *not* yet true (no SSR/data-fetching patterns adopted, no new pages).
- `vercel.json` — now carries a real rewrite: `/api/*` → `/api`. **Not**
  `{}` anymore (that was the old all-in-one-app model) — see
  `VERCEL_DEPLOY.md`'s 2026-10-29 update for exactly why the
  destination must be `/api`, not `/api/index` (the original 404 this
  guide was written to fix was this exact destination mistake, just
  under a different model).
- `kaggle_backtest/` — Kaggle kernel template for on-demand backtests
  (`KAGGLE.md`).

**Legacy (not part of current work):** `pyq_p3.py`, `p3_backtest.py`,
`pyquant_orchestra.py`, `pyquant_utils.py`, `.log` files — predate the
"p4" rewrite, kept for reference only. `legacy_web/` — the pre-2026-10-29
static HTML/CSS/JS frontend, superseded by the Next.js app above; kept
only for reference, not deployed.

**Housekeeping backlog:** `*.log` files are committed and growing — add
to `.gitignore` next time it's touched anyway (see `PLAN.md` backlog).

## 4. The three execution tiers

`walkforward.py` (validation, no capital) → paper (`PYQUANT_MODE=paper`,
Binance testnet) → live (`PYQUANT_MODE=live`, real capital). A strategy
change proves itself at each tier before the next. `SIGNAL_ONLY_MODE=true`
is an orthogonal flag — generates/logs/simulated-tracks with zero
exchange calls, overriding `TEST_MODE`/`trading_enabled` regardless of
`PYQUANT_MODE`. Full detail: `SPEC.md` Section 2.

## 5. Current status

**See `PLAN.md` for the live, checklist-level status** (phase table,
active items, done-log, decision-log) — that file is updated every
session and is the authoritative "what's done/active/next." This
section only orients a new session to where things stand structurally.

- **Deployed and working:** Vercel (`https://pyq-p3.vercel.app`),
  Supabase Postgres, GitHub Actions cron (`.github/workflows/
  orchestra.yml`, every 5 min), `SIGNAL_ONLY_MODE` (live on
  `origin/main`).
- **Known-broken right now, both config-only fixes, no code needed:**
  (1) `DATABASE_URL` was set to Supabase's Direct connection string,
  which is IPv6-only — GitHub Actions has no IPv6 egress, so every
  state-store write has been failing, including run-guards (which fail
  *open*, so every task has been running every 5 minutes instead of its
  real cadence — see `SUPABASE_SETUP.md`'s IPv6 incident note for the
  fix: switch to the Transaction pooler string). (2) The `kaggle`
  PyPI dependency was only listed in `api/requirements.txt`, which
  Vercel's framework-preset deployment doesn't read — every backtest
  trigger 502'd. Fixed in `pyproject.toml`.
- **Fixed, verified, not yet pushed:** the UKF per-tick-retraining bug
  (`DOCS.md` Section 10) and the Binance geo-block crash, in two rounds
  (`BINANCE_GEOBLOCK.md` — round 1 fixed client construction, round 2
  fixed `PositionGuard`'s hedge-mode detection after a live run found
  round 1 incomplete).
- **Former blocker, now resolved (confirmed 2026-10-25):** Claude's
  GitHub App previously had no push access to `Pyrex611/pyq-p3` (403 on
  every `git push`). A push attempted 2026-10-25 succeeded directly
  (commit `8ec5648`, confirmed via `git ls-remote`) — the access had
  been restored (or the earlier 403s were actually non-fast-forward
  rejections from a stale local clone, masked by a confusing combined
  error message; either way, push now works). Going forward, Claude
  commits and pushes directly instead of delivering a zip for manual
  apply — **but always `git fetch && git rebase origin/<branch>`
  first**, since the owner may apply changes by hand between sessions
  (exactly what happened here: `origin/main` had moved 3 commits ahead
  of this session's starting point) — a push without rebasing first
  risks a non-fast-forward rejection or, worse, a force-push silently
  discarding the owner's own manual changes. If a future push ever 403s
  again, fall back to the zip-delivery process below and re-check this
  note before assuming it's still broken.
- **Not yet done:** walkforward comparison validating the GitHub
  Actions UKF/tracker restart-safety; paper mode has never actually run
  end-to-end; real order placement needs a non-geo-blocked host
  (`BINANCE_GEOBLOCK.md` recommends residential/home IP, not a
  datacenter VPS).
- **Done this session (2026-10-29), not yet confirmed on a live
  deploy:** the Next.js frontend migration (Section 3's "Next.js
  frontend" entry) — same features/functionality/UI as the static site
  it replaced, `vercel.json` updated with the `/api/*` → `/api` rewrite
  this needs, `api/index.py`'s `StaticFiles` mount removed. Needs a real
  Vercel deploy to confirm the Framework Preset auto-detects as Next.js
  and the rewrite actually reaches the API function — see
  `VERCEL_DEPLOY.md`'s 2026-10-29 updates before assuming either works.
  Also shipped the same session: `GET /api/compare/daily` (day-bucketed
  Backtest-vs-Live data, `pyquant_state.compute_comparison_buckets()`),
  surfaced on the new Compare page.
- **Done 2026-10-29, second pass:** the primary (GitHub Actions)
  backtest path no longer depends on the Kaggle Dataset for historical
  data — `.github/workflows/backtest.yml` now calls `data_download.py`
  directly against Alpaca (same call `daily_optimizer()` already makes
  live), so a backtest run is never stale behind a manually-uploaded
  snapshot. No new secrets/dependencies needed (`ALPACA_API_KEY`/
  `ALPACA_SECRET_KEY` and `alpaca-py`/`python-dotenv` were already in
  place for `orchestra.yml`). The Kaggle Dataset is now only read by
  the Kaggle-kernel BACKUP path. See `PLAN.md`'s 2026-10-29 second-pass
  entry and `KAGGLE.md` Part J for the full writeup.
- **Fixed 2026-11-02 — root cause of "0 trades persisted" on real runs:**
  `pyquant_state.py` never imported `math`, but `_clean_val()` (added in
  the owner's independent `2fdf36b`) calls `math.isnan`/`math.isinf` on
  every float field — so every call to `record_backtest_trades()`/the
  new `record_capital_curve()` raised a bare `NameError` on the first
  float value it touched, outside any inner try/except, caught only by
  the caller's own best-effort wrapper in `run_backtest_gha.py`/
  `run_backtest.py` and silently logged as "(non-fatal)". This is why a
  completed run could report "267 total trades" in its metrics but 0
  rows in `pyquant_backtest_trades` (RunDetailModal's equity chart then
  fell back to a straight 2-point line, and the Executed Trades Log
  showed the generic "run predates per-trade logging" message on a run
  that didn't). Fixed by adding the import. Hardened on top of that:
  `record_backtest_trades()` now inserts rows individually instead of
  one `executemany()` call, so one future bad row can never drop an
  entire run's trades again; `trades_to_records()` never emits
  None for the NOT NULL `entry_price`/`side` columns either.
- **Added 2026-11-02:** real per-bar `capital_curve` persistence
  (`pyquant_backtest_curves` table, `record_capital_curve()`/
  `get_capital_curve()`, `GET /api/runs/{id}/curve`) — `walkforward.py`
  always computed this array but it was discarded after each run;
  `EquityCurveChart.tsx` now plots it (downsampled to 400 points) when
  present instead of reconstructing a curve from trades. Also fixed:
  `update_run_status()` now bumps `finished_at` on every terminal
  status (`completed`/`failed`), not only when `metrics` is passed —
  previously every failure call site left `finished_at` stuck at
  request-time, so a failed run's "Finished" timestamp was meaningless;
  and `RunDetailModal.jsx`'s Backtest Window & Runtime panel no longer
  shows a blank End Date/Test Window for a date-range run with no
  explicit `end_date` (that means "ran to the latest available data",
  a real resolved value — it now falls back to `bounds.latest_date` and
  computes Test Window from the resolved start/end dates).
- **Added 2026-11-03 — backtest-vs-live gate diagnostics, "PyQ Insights,"
  mode-toggle clarification, sticky tables, Bybit planning.** Committed
  together (see git log around this date); not yet confirmed on a live
  deploy.
  - **Backtest-vs-live comparison was previously impossible at the gate
    level**, not because the shared math differs (it's identical — see
    `pyquant_signal_math.py`) but because backtest's `generate_signal()`
    only ever recorded a diagnostic trace on bars where a signal fired;
    every non-firing bar left no trace to compare against live's
    continuous per-tick diagnostic history. Fixed by adding
    `diagnose_gates()` to `walkforward.py` (mirrors `generate_signal()`'s
    gate math, called every bar regardless of outcome) and a new
    `pyquant_backtest_diagnostics` table/`record_backtest_diagnostics()`/
    `get_backtest_diagnostics()`/`get_backtest_diagnostic_at()` in
    `pyquant_state.py` to persist and nearest-match it. Also found during
    this investigation: live's `daily_optimizer()` actually runs through
    `p4_search.py`, a separate undocumented duplicate of the optimizer
    (see Section 3) — numerically identical today, but a standing-rule
    violation and latent drift risk, not yet fixed.
  - New UI: Compare page's "Model State Comparison" card
    (`ModelStateComparison()` in `pages/compare.js`) — pick a
    symbol/timestamp/backtest run and see both models' gate state
    side-by-side for any hour, not just at a trade; backed by new
    `GET /api/compare/gate-state` (shares a `_nearest_gate_states()`
    helper with the existing trade-alignment endpoint, which also grew a
    `run_id` param and a `backtest_diagnostics` field).
    `RunDetailModal.jsx`'s "Compare to Live" drawer now shows the
    backtest's own gate state next to live's, instead of just backtest
    price/PnL next to live's gate state.
  - **"PyQ Insights"**: `lib/api.js`'s `pyqInsight()`/`pyqInsightTone()`
    translate a raw gate-diagnostic row into a plain-language line
    ("Volatility threshold CROSSED... strict entry gate has NOT
    crossed... waiting for retrace", etc). Surfaced via a new shared
    `components/GateReadout.jsx` (used by both `RunDetailModal.jsx` and
    `pages/compare.js`) and a new `PyqInsightsPanel` on
    `pages/live-monitor.js` (latest-tick explanation + tally by tone),
    replacing the old chart-with-no-explanation Signal Diagnostics
    History.
  - **LIVE/PAPER toggle "stuck on LIVE" is a config/doc gap, not a
    bug**: `PYQUANT_MODE` must be set as both a Vercel env var (API
    display) AND a separate GitHub Actions repo variable (what the
    orchestra actually reads) — see `VERCEL_DEPLOY.md`/
    `GITHUB_ACTIONS_SETUP.md`. The API never holds credentials, so this
    toggle is informational display only by design, not a live control
    — confirmed this is the trust boundary (Section 2) working as
    intended. Confirmed separately: PAPER mode always constructs the
    Binance client with `testnet=True` and separate testnet credentials
    (`p4_utils.py`), never touching mainnet. `ModeSwitch.jsx` now shows
    an explanatory tooltip on both buttons plus a
    "SIGNAL-ONLY · NO EXCHANGE CALLS" / "EXCHANGE CALLS ENABLED" badge
    driven by a new `signal_only_mode` field on `GET`/`POST /api/status`.
  - **Sticky table headers** added globally (`styles/globals.css`) via a
    `--sticky-table-top` custom property, 64px on page-level tables
    (below the fixed topbar) and re-scoped to 0px inside
    `.modal-overlay`/`.modal-card` (a modal scrolls its own content with
    no topbar in that context). Verified this correctly nests inside
    `RunDetailModal.jsx`'s Executed Trades table, which has its own
    `overflowY: auto` scroll box — `position: sticky` sticks to the
    nearest scrolling ancestor, so inside that box `top: 0` is the
    already-correct behavior.
  - **Executed Trades now defaults to most-recent-first** (was
    oldest-first, the API's raw `ORDER BY id ASC`), with a sort control
    for Oldest First / Return % (both directions) / Drawdown Risk. A
    true per-trade "sort by volatility" was requested but not built:
    volatility is a per-bar diagnostic, not a per-trade field, so
    sorting trades by it would mean arbitrarily picking one bar (e.g.
    entry-tick) per trade — the table instead notes this and points at
    "Compare to Live" / Model State Comparison for actual gate values.
  - **Bybit/multi-provider**: planning-only backlog entry added to
    `PLAN.md` per explicit instruction not to start any code changes
    yet.
- **Added 2026-11-03, second pass — shadow signal comparison + exact-
  hour matching fix.** Owner's own follow-up after reviewing the gate
  comparison above: fix the comparison tool's timestamp matching, and
  let live trading optionally run on walkforward.py's own optimizer
  instead of (or alongside, as a non-trading shadow) `p4_search.py`'s.
  - **Exact-hour matching**: `_nearest_gate_states()` (api/index.py)
    and `get_backtest_diagnostic_at()` (pyquant_state.py) now require
    the candidate row to fall in the SAME UTC calendar hour as the
    target timestamp, not just "within 90 minutes" — a plain nearest-
    match could (and, in a real example, did) pair a backtest bar with
    a DIFFERENT live hour's reading, showing up as a false "trend
    disagreement" that looked like a model divergence but was actually
    a matching bug. See `_same_utc_hour()`'s docstring in api/index.py.
  - **`walkforward.run_standalone_grid_search()`** (new): a self-
    contained entrypoint around walkforward.py's OWN optimizer (the
    same one `run_walk_forward()` already uses internally), exposed
    with the same zero/few-arg calling convention as
    `p4_search.run_grid_search()` — reads the CSVs, trains its own
    UKFs, needs nothing external. This is what makes "run either
    optimizer and compare" possible without duplicating more math.
  - **`live_signal_source` kv setting** (`p4_search` default |
    `walkforward`) — read fresh on every `generate_signal()` call via
    `SignalGenerator.load_params()` (p4_utils.py), so flipping it takes
    effect on the very next tick. This is the ONLY thing that changes:
    the gate/order-construction math in `generate_signal()` is
    unchanged either way — only which stored params dict it loads
    differs. Falls back to `p4_search`'s params if `walkforward`'s
    don't exist yet (never trades on empty/garbage thresholds).
  - **`shadow_comparison_enabled` kv setting** (default **off**) — when
    on, `p4_orchestra.py`'s nightly `daily_optimizer()` ALSO runs
    `run_standalone_grid_search()` (own run-guard, `shadow_optimizer`
    bucket, stores to kv key `walkforward_optimal_params`), and
    `pyq_p4.py`'s `hourly_task()` ALSO computes a read-only diagnostic
    for the non-selected source every tick via
    `walkforward.diagnose_gates()` (never builds a tradeable signal, so
    it can never place an order). Off by default because it roughly
    doubles the nightly optimizer's runtime — see
    `_run_shadow_param_search()`'s docstring for the 2026-10-27 GitHub
    Actions 10-minute-timeout incident this guards against.
  - **`pyquant_diagnostics_history` gained a `source` column**
    (`p4_search` default | `walkforward`) so both sources' rows can
    coexist without corrupting each other's trend charts —
    `GET /api/diagnostics/history` takes an optional `source` filter;
    Live Monitor fetches the live-selected source for the main chart
    and, when shadow comparison is on, the other source separately for
    a clearly-labeled "Shadow" panel, rather than one unfiltered call
    that would zig-zag two optimizers' readings together.
  - **New `GET`/`POST /api/signal-source`** — reads/writes both kv
    settings; still never gives the API itself the ability to place a
    trade (same one-flag-the-orchestra-reads pattern as
    `trading_enabled`). Settings page gained a card for this with an
    explicit warning that switching `live_signal_source` changes what
    real orders are sized/fired on.
  - **Not yet done**: this hasn't run in production — `walkforward_
    optimal_params` won't exist until shadow comparison is turned on
    and a `daily_optimizer()` cycle completes. Until then, switching
    `live_signal_source` to `walkforward` is a no-op (falls back to
    `p4_search`'s params, by design — see above).
- **Added 2026-11-04 — manual optimizer trigger + staggered automatic
  schedule.** Owner follow-up: a button to run either optimizer on
  demand, and confirmation that the two heavy optimizers don't have to
  (and, per the 2026-10-27 timeout incident, really shouldn't) run in
  the same process.
  - **Automatic schedule now staggered by clock time, not just order**:
    `_run_daily_param_search()` (p4_search) only proceeds at/after
    01:00 UTC; `_run_shadow_param_search()` (walkforward) at/after
    01:30 UTC — each gets its OWN GitHub Actions cron tick/10-minute
    budget instead of sharing `daily_optimizer()`'s single call. The
    cheap UKF re-anchor is unaffected (still fires immediately after
    midnight). This is a real timing change from "ASAP after
    midnight" to "01:00 UTC" for the primary optimizer — deliberate,
    not a regression, done specifically to end the back-to-back-in-
    one-process pattern the 2026-10-27 incident warned about.
  - **`manual_optimizer_request` kv flag** (`p4_search`/`walkforward`
    booleans) + new `GET`/`POST /api/optimizer/trigger` — Settings
    page's "Run Optimizer Now" buttons (P4_SEARCH / WALKFORWARD /
    Both). Same trust-boundary pattern as `trading_enabled`: the API
    only sets a flag, never runs the optimizer itself — the next
    GitHub Actions cron tick (~5 min) is what actually executes it, via
    `p4_orchestra.py`'s new `_run_pending_manual_optimizer()`.
  - **At most one optimizer runs per tick, even for manual triggers**:
    requesting "Both" queues both flags, but
    `_run_pending_manual_optimizer()` only honors one per tick
    (p4_search first) — the other stays queued for the next tick
    (~5 min later), preserving the same never-share-a-process
    invariant the automatic schedule's 01:00/01:30 split relies on.
  - A manual run bypasses the clock gate, the day-bucket guard (via a
    one-off `manual-<minute>` bucket — `try_claim_run()`'s existing
    primitive, no schema change), and (for walkforward) the
    `shadow_comparison_enabled` toggle — but does NOT consume the
    day's automatic slot, so it's purely an extra one-off run, never a
    substitute for the scheduled one.
- **FIXED 2026-11-05 — `data_download()` TimeFrame crash, real outage
  window ~3 hours (CORRECTED 2026-11-06 — see note at the end).**
  `p4_utils.py`'s `data_download()` built its `tf_map` dict with
  `"15T": TimeFrame(15, TimeFrame.Minute)` and `"5T": TimeFrame(5,
  TimeFrame.Minute)` — passing the `TimeFrame.Minute` PRESET (itself a
  `TimeFrame` instance, amount=1) as the `unit` argument, where
  alpaca-py requires a `TimeFrameUnit` enum member. Because `tf_map` is
  a dict literal, every value is evaluated unconditionally on every
  call — so once triggered, `data_download()` raised on EVERY call, for
  EVERY timeframe, not only "15T"/"5T". The line itself dates to
  `b0e588d` (owner, 2026-10-20), but **it was latent, not active, from
  2026-10-20 to 2026-11-05**: `requirements.txt` pins `alpaca-py>=0.20.0`
  (open-ended), so each GitHub Actions run does a fresh `pip install`
  that floats to whatever's newest on PyPI. Confirmed via actual run
  logs (`gh run view --log` across Oct 20–Nov 4): every tick in that
  window installed **alpaca-py 0.44.0**, which did not validate
  `unit` strictly, so the bad line was harmless — `one_minute_task()`
  logs show it downloading real ETH/USD and BTC/USD bars successfully
  throughout. The crash only started when a run picked up **alpaca-py
  0.45.0** (confirmed first appearing in a run's install log on
  2026-11-05), which added the strict check that turns this into
  `TypeError: Unit must be a TimeFrameUnit value, not a TimeFrame...`.
  **Real blast radius, confirmed from GitHub Actions' own run history**:
  a run-level `concurrency` group (queue depth 1, no cancel-in-progress)
  meant `one_minute_task()`'s uncapped `while data is None:` retry loop
  held each run "in progress" far longer than the 5-minute cadence, so
  every subsequent queued tick got superseded/cancelled — a visible
  streak of ~38 "cancelled" runs from **2026-11-05 19:00 UTC to 22:10
  UTC** (confirmed via the GitHub API), ending at the first "success"
  run at **22:15 UTC**, 10 minutes after this fix was pushed (22:05:19
  UTC). So the real outage was **about 3 hours**, not weeks. Fixed by:
  (1) importing `TimeFrameUnit` and using `TimeFrame(15,
  TimeFrameUnit.Minute)` / `TimeFrame(5, TimeFrameUnit.Minute)`; (2)
  adding a 5-attempt retry cap (`continue` to the next symbol/tick on
  exhaustion) to all four `while ... is None:` download loops in
  `pyq_p4.py` — defense in depth so a future transient failure degrades
  to "skip a symbol this tick" instead of queuing/cancelling ticks for
  hours. Verified by installing alpaca-py fresh in the dev sandbox and
  reproducing both the exact failure (old code) and the fix (new code)
  against the real library.
  **CORRECTION NOTE (2026-11-06):** the original version of this entry
  claimed the whole cron pipeline had been "silently dead since
  2026-10-20" / "~2.5 weeks of total scheduler paralysis," reasoning
  from the code alone (an uncapped retry loop hitting a
  deterministic-looking crash) without checking the actual GitHub
  Actions run history first. That was wrong, as the real run logs
  above show — flagging this as a reminder to check `gh run list`/
  `gh run view --log` against the real history before writing an
  incident's blast-radius into this file, not just reasoning from
  reading the code.
- **Added 2026-11-06 — expanded live-vs-backtest comparison to full raw
  values + a training-window + variance column.** Owner follow-up to
  the Compare page's gate comparison: the existing cards only showed
  gate PASS/FAIL verdicts (via `GateReadout`), not the underlying
  numbers, so there was no way to tell a genuine model divergence from
  ordinary UKF-state noise at a borderline gate reading. Also: an
  explicit scrutiny pass over the direction/trend-extraction code
  (`generate_signal()` vs `diagnose_gates()`, UKF argument ordering
  through `step_ukf()`/`ukf_handler()`/`ukf_mapping`/
  `_persist_ukf_state()`/`_restore_ukf_state()`) found **no swap or
  sign-flip bug** — every call site is internally consistent
  (close/high/low ordering never crossed). What WAS missing, confirmed
  by reading `diagnose_gates()` next to `generate_signal()`'s
  `last_diagnostic`: backtest's diagnostic dict never captured
  `high_pred`/`low_pred`/`buy_price`/`sell_price` at all — only the
  derived gate verdicts — so a true side-by-side of the raw band values
  was never possible from stored data, independent of any UI.
  - **`walkforward.py`'s `diagnose_gates()`** now sets `high_pred`/
    `low_pred` in its base dict and `buy_price`/`sell_price` in the
    up/down branches, mirroring `generate_signal()` exactly.
  - **Training-window metadata, both sides** — new, previously never
    captured anywhere: `walkforward.py`'s `paper_trade_day()` takes
    optional `btc_train_meta`/`eth_train_meta` (`{start, end,
    bar_count}`, computed from `btc_train`/`eth_train`'s index in
    `run_walk_forward()`'s per-day loop) and stamps
    `train_window_start`/`train_window_end`/`train_bar_count` onto
    every diagnostic row. `pyq_p4.py`'s `retrain_daily_ukfs()` captures
    the same three values from `bars.index` **before** the
    `reset_index(drop=True)` that was already there (which would
    otherwise discard the DatetimeIndex) and persists them to a new kv
    key (`pyq_p4_ukf_retrain_meta`, by symbol); `_persist_signal_diagnostic()`
    reads that kv and stamps the same three fields onto every live
    diagnostic row. Purely descriptive — never read back into any
    trading decision, and if a given hour's values don't line up
    between live and backtest, that's the signal the rest of that
    row's differences are expected, not a bug.
  - **Schema**: both `pyquant_diagnostics_history` and
    `pyquant_backtest_diagnostics` gained 7 nullable columns
    (`high_pred`, `low_pred`, `buy_price`, `sell_price`,
    `train_window_start`, `train_window_end`, `train_bar_count`) via
    `ALTER TABLE ADD COLUMN IF NOT EXISTS` (Postgres) / try-except
    `ALTER TABLE` (SQLite) — placed AFTER both tables' own `CREATE
    TABLE` (the 2026-11-02 ordering mistake this file already warns
    about, not repeated here). `_DIAG_HISTORY_COLS`/`_BACKTEST_DIAG_COLS`
    drive both the INSERT and SELECT column lists, so `api/index.py`'s
    `_nearest_gate_states()` needed **no changes** — the new fields
    just ride along in the same dicts it already returns.
  - **New shared `components/FullComparisonTable.jsx`**: a Field |
    Backtest | Live | Diff table — every numeric field gets an absolute
    AND relative (%) diff (flagged in red past 5%), every text/bool
    field gets a MATCH/DIFFER verdict. Used by both `pages/compare.js`'s
    `ModelStateComparison` (below the existing `GateReadout` pair) and
    `components/backtest/RunDetailModal.jsx`'s "Compare to Live" drawer
    (below its existing trade/gate panels) — the same two places that
    already shared `GateReadout`, same pattern.
  - **Verified**: `diagnose_gates()`'s new fields directly; a full
    SQLite round-trip of both `record_diagnostic_history()`/
    `record_backtest_diagnostics()` through `list_diagnostic_history()`/
    `get_backtest_diagnostic_at()` with every new field populated; the
    `bars.index` capture snippet in isolation; `npm run build` (clean,
    all 8 pages). Not yet confirmed on a live deploy.

Granular, current backlog: `PLAN.md`. Bigger architecture options below
are stable enough to live here rather than churn `PLAN.md`:

- **Backtest-from-UI (Kaggle)** — done, not yet pushed; see `KAGGLE.md`
  for the why/how and `PLAN.md` for what's left (one-time Kaggle
  account/kernel/dataset setup). Confirmed: the hourly/live
  signal-generation loop does **not** need Kaggle — only the heavy,
  on-demand backtest/walkforward workload does (Vercel has neither the
  time limit nor a durable filesystem for it; Kaggle has a public,
  programmatic trigger API that Colab's free tier lacks).
- **Vercel + QStash re-architecture** (eliminate GitHub Actions' 5-min
  cron floor) — brainstormed in `VPS_SETUP.md`'s free-alternatives
  section; the GitHub Actions migration was the first step toward this
  (externalizing engine state). Deferred until the GitHub Actions path
  is confirmed stable. Would require revisiting the API's no-credentials
  trust boundary (Section 2) since these functions would call
  Alpaca/Binance directly — a real architectural change, not routine.
- Smaller candidates (on-demand `daily_optimizer()` trigger, editable
  settings, per-symbol pause): tracked in `PLAN.md`'s backlog.
- **`p4_search.py` consolidation** (found 2026-11-03, Section 3/5) —
  either make `daily_optimizer()` call `walkforward.py`'s own search
  directly, or have `p4_search.py` import its math constants from
  `pyquant_signal_math.py` instead of redefining them, so the two
  can't silently drift. Not urgent (constants match today) but should
  happen before the next shared-math change.
- **Bybit / multi-provider support** — planning only so far; see
  `PLAN.md`'s backlog entry for the full scope (provider abstraction,
  credential separation, per-symbol exchange selection, cost-model
  constants, geo-block check). No code changes made toward this.

## 7. Standing rules (apply without being re-asked)

- **Zip deliveries:** only new/modified files, full unabridged content,
  never a whole-repo re-zip (explicit standing feedback). Matters more
  now that the owner applies/pushes these by hand.
- **Push-failure safety net (2026-11-04):** if `git push` fails on its
  first attempt for any reason, zip all modified files for that commit
  and send them to the owner via `SendUserFile` BEFORE any subsequent
  or parallel retry of that push — so the owner always has local access
  to the pending work regardless of git's state. Only after the zip is
  sent, retry the push. Applies to every future push in this project,
  not just the incident that prompted it (a transient GitHub "Internal
  Server Error" on commit `2ebbe0e`, which succeeded on the next retry
  after the zip was sent).
- **"Testing deferred to integration"** means the live trading engine
  specifically (`pyq_p4.py`/`p4_orchestra.py` against a real exchange).
  Ordinary software testing of non-trading infrastructure (state-store
  unit tests, `TestClient`, headless-browser UI passes) is in scope and
  expected.
- **Shared math changes go in `pyquant_signal_math.py` only** — never
  patch `walkforward.py`/`p4_utils.py` independently.
- **State-store writes from the engine are best-effort** — try/except,
  fail open, never let logging touch a trading decision.
- **Granular setup/procedure docs live in this repo's `.md` files**, not
  the Claude Docs plan doc (kept to dev-plan-level content only). New
  how-to → new/extended `.md` file here, linked from Section 8.
- **Setup docs must be maximally granular** — account/project creation,
  exact UI button names and field order, not just non-obvious steps.
- **The API never holds exchange credentials or calls Binance/Alpaca
  directly** — read layer + one off-switch write, always.
- **Repo is public** — credentials only in GitHub Secrets/Vercel env
  vars/the VPS's `.env`, never in code or a committed file. Any new file
  that dumps runtime state goes in `.gitignore` in the same change.
- **Any new task called from `cron_runner.py` must self-guard** with
  `state_store.try_claim_run()` — it calls every task unconditionally
  every tick.
- **A dependency used by `api/index.py` must be listed in
  `pyproject.toml`'s `dependencies`, not only `api/requirements.txt`**
  (that file is a human-readable manifest only, per the 2026-10-19
  incident — see Section 5).

## 8. Document map

- **`CLAUDE.md`** (this file) — start here.
- **`INTENT.md`** — why this exists, who it's for, non-goals, risk
  posture. Changes rarely.
- **`SPEC.md`** — structured current-state reference: components, tiers,
  trust boundary, data model, config, failure modes, non-functional
  requirements, known gaps.
- **`PLAN.md`** — checklist-level roadmap: phase status, active items,
  backlog, done-log, decision-log. Update every session.
- **`DOCS.md`** — architecture/code reference and incident technical
  writeups (UKF fix, Kaggle integration design).
- **`BINANCE_GEOBLOCK.md`** — Binance geo-block: root cause (both
  rounds), fix, hosting recommendation.
- **`SUPABASE_SETUP.md`** — DB provisioning, `DATABASE_URL` (pooled, not
  direct — see its IPv6 incident note), confirming tables exist.
- **`GITHUB_ACTIONS_SETUP.md`** — orchestra hosting (current path):
  secrets, `PYQUANT_MODE`/`SIGNAL_ONLY_MODE` variables, public-repo step.
- **`VPS_SETUP.md`** — orchestra hosting (fallback path) + free-tier
  alternatives brainstorm.
- **`VERCEL_DEPLOY.md`** — API+frontend deploy, env vars, the 404
  root-cause writeup.
- **`KAGGLE.md`** — backtest-from-UI: why Kaggle, one-time setup,
  testing, known limitations.
- **Claude Docs plan doc** (`https://claude.ai/code/artifact/2a54f458-6ebe-423b-b4c8-e2deac599d7c`)
  — original strategy/performance deep-dive, dev-plan level only;
  `CLAUDE.md`/`PLAN.md` are the current source for status.
- **`.env.example`** — full environment variable list.

## 9. Keep this file current

Update Section 5/6/7 in the same turn as any change that affects them —
don't let this drift from reality. Prefer updating `PLAN.md` for routine
progress and keeping this file's Section 5 to short status + pointers
(per the 2026-10-19 condensation pass); only promote something here when
it's stable enough to not need weekly rewording.

## 10. Resuming in a new session — quick checklist

1. Read this file, then `PLAN.md`'s "Active" section (fastest answer to
   "what was in progress").
2. Check `git log --oneline -5` / `git status` — local and `origin/main`
   can genuinely diverge (Section 5's push blocker).
3. Check `INTENT.md`/`SPEC.md` before guessing on an ambiguous design
   decision.
4. Pick up `PLAN.md`'s top "Active" item unless redirected.
