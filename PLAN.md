# PLAN.md — Roadmap, status, and decision log

**Last updated:** 2026-10-28, second pass same day. Follow-up to the
previous pass's datetime-bug fix (owner confirmed the Dashboard now
shows %PnL and a new signal again — the fix worked). Four more items:

1. **%PnL leverage, confirmed by reading the code (not assumed):**
   every `realized_pnl_pct` IS leveraged — `pyquant_signal_math.outcome_pct()`
   returns `raw_price_move × leverage`, and `pyq_p4.py`'s
   `_check_signal_only_closes()` calls it with no explicit `leverage`
   argument, so it always uses `LEVERAGE_DEFAULT` (10×). Added a
   display-only "Leverage ×" control to the Dashboard (`web/index.html`)
   that re-derives each figure from the same stored
   `entry_price`/`outcome_price`/`side` at any chosen leverage, entirely
   client-side — no backend change, and it never touches what the
   engine actually trades/simulates at. Scope-limited to the rows the
   Dashboard already has prices for (`recent_closed`, up to 10) —
   labeled as such rather than silently claiming to match the backend's
   200-row aggregate.
2. **Fixed a real, confirmed-live bug the owner hit testing the
   GitHub Actions backtest path**: `ModuleNotFoundError: No module
   named 'pyquant_state'` in `run_backtest_gha.py`. Root cause:
   `python3 kaggle_backtest/run_backtest_gha.py` puts that SCRIPT's own
   directory at `sys.path[0]`, not the invoking shell's cwd (the repo
   root) — so `import pyquant_state`/`import walkforward`, both at the
   repo root, never resolved, regardless of the workflow's working
   directory. Fixed with an explicit `sys.path.insert()` for the repo
   root at the top of the script.
3. **Backtest flow restructured to the settled design**: "Run Backtest"
   now pushes to the Kaggle kernel (what "Sync to Kaggle" used to do)
   and reveals a new "Run on Kaggle →" button/link (opens the kernel in
   a new tab) instead of triggering anything remotely. The GitHub
   Actions trigger (item 2's fix) is kept, demoted to a collapsed
   "Advanced" section — the owner's reported Kaggle-dataset-visibility
   and import-path issues made the manual, full-console-visibility
   Kaggle path the preferred default, not GitHub Actions.
4. **Started the per-trade backtest data work** (the prerequisite
   PLAN.md's 2026-10-27 Decisions entry named): new
   `pyquant_backtest_trades` table (run_id FK, symbol/side/entry+exit
   price/tp/sl/qty/exit_leg/profit/pnl_pct/entry+exit timestamp),
   `pyquant_state.record_backtest_trades()`/`get_backtest_trades()`,
   and `walkforward.py`'s new `trades_to_records()` (normalizes its own
   internal trade-dict shape, computes `pnl_pct` via the same
   `outcome_pct()` leveraged formula used live, so it's directly
   comparable to a live signal's `realized_pnl_pct`). Wired into all
   three places a walkforward run can finish: plain CLI usage,
   `run_backtest.py` (Kaggle manual), `run_backtest_gha.py` (GitHub
   Actions). New `GET /api/runs/{id}/trades` endpoint, and the Backtest
   page's run-detail modal now shows a per-trade table when present —
   not yet wired into the Compare page itself (still v1/aggregate-only
   there; this is the data foundation for a future v2, not the v2 UI).

**New standing architecture note** (owner's explicit instruction,
2026-10-28): the "Compare menu only shows on some pages" class of
report is exactly what a Next.js (or similar component-based) migration
would prevent structurally (one shared layout component instead of
N copy-pasted `<nav>` blocks). Decided NOT to invest further in
copy-paste-discipline tooling for this static-HTML frontend going
forward — see the new Backlog entry below for the full reasoning this
should carry into future sessions' architecture choices.

**Previous update, 2026-10-28, first pass same day.** **Verified (not
theorized) root cause found
for the Settings-page "Start Fresh From Now" baseline appearing to do
nothing** (the owner reported the Dashboard still showing a stale "$6"
figure even after clicking it and after the previous pass's self-heal
fix). Confirmed by directly querying the live `GET /api/dashboard` and
`GET /api/compare` endpoints: `signals_baseline_at` correctly showed a
just-set, recent timestamp, yet `recent_closed`/`trade_count` still
included every historical signal from days earlier, completely
unfiltered — proof the baseline comparison itself was silently a no-op
in production, not a caching or propagation issue (ruled out separately
by fetching with cache-busting query params and getting identical
results).

**Root cause, confirmed by reading the code with that behavior in
mind:** `pyquant_state.list_signals()` returns `dict(zip(cols, row))`
straight from the DB cursor with no type normalization. On SQLite (this
project's local/dev backend) a `TIMESTAMP` column comes back as a plain
`str` — so `datetime.fromisoformat(row["opened_at"])`, used by both
`api/index.py`'s `_after_baseline()` and `compute_live_performance()`'s
`since` filter, works fine and was presumably tested/written against
that backend. On **Postgres** (production, via psycopg2) the same
column comes back as a native `datetime.datetime` object —
`datetime.fromisoformat()` raises `TypeError: fromisoformat: argument
must be str` on that input, which both call sites' bare `except
Exception: return True` / `except Exception: pass` swallow silently,
degrading to "treat as in range" — i.e. count everything, baseline
filtering fully defeated, in production only. A classic backend-parity
trap this project has hit before (the IPv6 `DATABASE_URL`/Neon
incidents) — SQLite-vs-Postgres behavioral differences that only show
up live. **Fixed at the source**: `list_signals()` (and `_row_to_run()`,
defensively, for the same column-type risk on `pyquant_runs`) now
normalizes any `datetime` object to its ISO string before returning, so
every consumer sees a consistent type regardless of backend.

**This same bug was ALSO silently defeating `_check_signal_only_closes()`'s
multi-bar TP/SL lookback** (`pyq_p4.py`'s `_fetch_bar_extremes_since()`,
built 2026-10-25 specifically to fix signals getting stuck open for
days) — it's fed `row.get("opened_at")` from the exact same
`list_signals()` call, hit the exact same `TypeError`, and fell back
silently to **single-bar-only** checking (its own documented fallback
behavior for "any error") on every tick in production, the entire time
it's been deployed. That's the OLD, narrower check this feature was
built to replace, quietly back in effect. Combined with `hourly_task()`'s
"already has an open position — skipping new signal generation" gate
(`pyq_p4.py` ~line 1361), a position that rarely closes (because its
extended lookback silently never ran) directly blocks new signal
generation for that symbol for as long as it stays open — a second,
now-confirmed, concrete mechanism by which signal generation can stall
for days, independent of the UKF-retrain-starvation fix below. Same
source-level fix in `list_signals()` resolves this too.

**Re-examined under scrutiny, ruled out as a cause:** `check_open_position()`
(`p4_utils.py`) — the function actually used by the current `pyq_p4.py`
stack — defaults `is_open_position` to `False` and only overwrites it
on a successful Binance read; on the geo-block exception seen in the
owner's GitHub Actions log (`'NoneType' object has no attribute
'futures_position_information'`), it logs and returns that same
all-`False` default. Confirmed by reading the function directly: this
does **not** cause `already_open` to default to `True` and block signal
generation — the geo-block errors in that log are noise for
`SIGNAL_ONLY_MODE` purposes, not a hidden gate. Flagging this
explicitly because it was a live suspect that scrutiny (not assumption)
cleared.

**Daily UKF-retrain-starvation fix (previous pass, below) status
downgraded from "confirmed root cause" to "a real, independently-fixed
bug of unconfirmed total impact":** the timeout/ordering flaw described
below is real and worth having fixed regardless, but nothing in this
session's evidence actually proves it fired on the specific days in
question — that was inferred from the log snippet's shape, not
confirmed from `pyquant_runs`/Actions run durations. Both fixes ship
together; only the datetime bug above is independently verified from
live data.

**Compare-menu-only-on-Settings-page report: investigated and NOT
reproduced.** Fetched the live, deployed `index.html` and
`live-monitor.html` directly (not from this repo's working copy) and
both already render "Compare" in the nav identically to every other
page. Almost certainly a stale browser/tab cache on the owner's end
from before the previous pass's deploy finished propagating — no code
change made here since there's nothing to fix; suggested a hard
refresh instead of a speculative "fix" for a problem not present in
what's actually being served.

**Previous update, 2026-10-27, second pass same day.** Live-incident root
cause found: **no new signal generated for ~3 days.** Owner's report
plus a GitHub Actions log from the top of an hour confirmed the daily
UKF re-anchor was the suspect (`"Persisted UKF state found — using
cheap shells (no Alpaca fetch/retrain this tick)"` on every tick, plus
`"[Orchestra] daily_optimizer already ran (or is running) today.
Skipping."`). Root cause: `p4_orchestra.py`'s `daily_optimizer()` wrapped
BOTH the fast 1H-UKF re-anchor (`pyq_p4.retrain_daily_ukfs()`, called
LAST) and the slow Bayesian/grid parameter search (up to 150 Optuna
trials over a year of hourly data × 2 symbols) in one try/except behind
ONE run-guard claimed up front. `orchestra.yml`'s job has a (then-)
10-minute timeout; if the search alone ran past it, GitHub Actions kills
the whole process outright — not a Python exception, nothing any
try/except here ever saw — AFTER the day's guard was already claimed
and BEFORE the re-anchor ran. Every tick for the rest of that day then
hit "already ran… skipping", and the live 1H UKFs went un-re-anchored —
exactly the slow-drift failure mode that stops signal generation, with
nothing in the UI pointing at why. **Fixed:**
1. `daily_optimizer()` split into two independently-guarded,
   independently-try/excepted steps (`_run_daily_ukf_retrain()`,
   `_run_daily_param_search()`), re-anchor FIRST — a param-search
   timeout can no longer starve it.
2. Both write a result (success or failure) somewhere this platform
   actually reads: the re-anchor writes a new
   `pyquant_ukf_retrain_heartbeat` KV row, surfaced on `GET
   /api/heartbeat`'s new `ukf_retrain`/`ukf_retrain_age_seconds` fields
   and the Live Monitor page's Engine Heartbeat card (so "is the UKF
   being retrained" is now a glance, not a log dig); the param search
   writes a real `failed` `pyquant_runs` row via `request_backtest()` +
   `update_run_status()` on exception, instead of a print easy to miss.
3. `orchestra.yml`'s job `timeout-minutes` raised 10 → 25, giving the
   search itself more room to actually finish (tradeoff: a slow day can
   now occupy the `pyquant-orchestra` concurrency group — and so delay
   the 5-minute tick cadence — for up to 25 minutes instead of 10; judged
   worth it since the re-anchor no longer depends on the search
   finishing at all).
4. Separately: the Dashboard's lingering pre-fix "$6" figure (reported
   still showing even after the previous push) was the signals-baseline
   feature requiring an opt-in click the owner reasonably didn't know to
   make. `pyquant_state.get_signals_baseline()` now self-heals to now()
   the FIRST time it's ever read if no baseline row exists at all yet —
   an explicit "Clear" (Settings page) still means "count everything"
   and is never overridden by this. No manual step needed going forward.

**Confirmed already correct, no change needed:** `web/backtest.html`'s
"Sync to Kaggle" button (shipped in the previous pass) already just
pushes the kernel and links to it for the owner to open and click "Save
& Run All" themselves — it never calls any Kaggle run/execute API, so
"don't try to trigger the backtest remotely" was already how it worked.

**Not yet done / next priorities**, per the owner's explicit ask to
prioritize signal-generation/UKF correctness first, then a fuller
platform review:
- **Live-test this fix**: needs a real GitHub Actions run of
  `daily_optimizer` to confirm the re-anchor now lands even if the
  search is slow, and the new Live Monitor heartbeat line renders. Not
  verifiable from this session (no live DB/Actions access).
- **Broader platform review** (asked for, not yet done in full): a
  pass over `pyq_p4.py`/`p4_utils.py`/`walkforward.py` specifically
  checking live-vs-backtest parity beyond the 1H UKF (e.g. whether
  15T/5T UKFs — never re-anchored by `retrain_daily_ukfs()` either,
  by design, only incrementally `.predict()`/`.update()`'d — drift
  differently live than in `walkforward.py`'s fresh-per-window
  training; whether `SignalGenerator.load_params()` ever actually picks
  up a day's new `optimal_params` write in practice).
- **Data pipeline cleanup** (asked for): a pass confirming every
  incoming tick's data is captured/stored/displayed with nothing
  silently dropped — deferred pending the above being confirmed fixed
  first, per the owner's stated priority order.

**Previous update, 2026-10-27, first pass same day.** Backtest "reform"
pass, per the owner's
2026-10-26 request to rework backtest persistence/comparison "without
breaking what already works." Five pieces, all code-complete:

1. **Signals baseline ("fresh start").** New `pyquant_kv` marker
   (`pyquant_signals_baseline`, `pyquant_state.get/set/clear_signals_baseline()`)
   lets the Dashboard's aggregate figures (open positions, recently
   closed, avg. return) start counting from "now" without deleting any
   history — the Signals and new Compare page always show everything
   regardless. Settings page got a new "Signals Baseline" card with
   "Start Fresh From Now"/"Clear" buttons; `GET/POST /api/signals/baseline*`
   endpoints. Exists specifically so a tracking/PnL-logic change (like
   the 2026-10-25 TP/SL fix) doesn't make old and new numbers mix
   together misleadingly.
2. **PnL display, confirmed correct in production.** The "dashboard
   still shows USD PnL" report turned out to be a stale/pre-deploy
   screenshot, not a bug — `GET /api/dashboard` already returns real
   `avg_pnl_pct_recent`, and the backlog of previously-stuck signals
   closed correctly with sane percentage values right after the earlier
   TP/SL fix deployed. No code change needed here this round.
3. **Run-detail modal (Backtest page).** Clicking any Walkforward/Daily
   Optimizer run row now opens a modal (`web/backtest.html`'s new
   `#run-modal`) with the full `metrics`/`params` payload, error, and
   Kaggle reference — replacing the old always-visible raw-JSON card.
4. **Kaggle manual-trigger workflow, rewritten.** `kaggle_backtest/
   run_backtest.py` no longer reads a `params.json` — parameters are
   now inline Python constants between `# ── RUN PARAMETERS ──` /
   `# ── END RUN PARAMETERS ──` markers, editable directly in Kaggle's
   own editor before a manual "Save & Run All". `api/index.py`'s
   `_push_kaggle_kernel()` was rewritten to match (string-replaces that
   same block with real values via the new `_render_run_params_block()`
   helper, instead of writing `params.json`), and a new endpoint,
   `POST /api/backtests/sync-to-kaggle`, plus a "Sync to Kaggle" button
   on the Backtest page, drive it — separate from `POST /api/backtests`
   (still the GitHub Actions path / default "Run Backtest" button). See
   `KAGGLE.md` Part K.
5. **Backtest-vs-Live comparison page.** New `web/compare.html` (added
   to every page's nav) renders `GET /api/compare`'s aggregate stats
   side by side: live performance (`pyquant_state.compute_live_performance()`,
   respecting the same baseline as the Dashboard) vs. the latest
   completed walkforward run's metrics. v1 is aggregate-only by design
   — see the page's own "Brainstorm" card (and this file's Decisions
   section) for the per-trade version's prerequisites.

**Not yet done from this pass:** live testing of "Sync to Kaggle" and
the Compare page against real data (code-complete, pushed, unverified
live — same caveat as the GitHub Actions backtest path below, which
also still needs its one-time GitHub/Vercel secret setup before a live
test is possible).

**Previous update, 2026-10-26.** Kaggle 409 was fixed (confirmed: the
owner deleted the old kernel and the next push created a correctly-
typed script kernel), but the backtest **still failed** — and Kaggle's
own UI showed "Failed" with no error log or detail at all, nothing to
act on, not even a traceback. Undebuggable from this session (no
programmatic way to inspect a Kaggle kernel's execution log here).
Rather than keep guessing at an opaque sandbox, moved backtest
**execution** to GitHub Actions instead: new `.github/workflows/
backtest.yml` (`workflow_dispatch`, triggered by `api/index.py`'s new
`_trigger_github_backtest()` via GitHub's REST dispatch API — same PAT
mechanism as `GITHUB_ACTIONS_SETUP.md` Part G's cron pinger) runs a new
`kaggle_backtest/run_backtest_gha.py`, which is `run_backtest.py`'s same
`walkforward.py`-calling logic minus the Kaggle-kernel-specific bits
(no clone — `actions/checkout` already has the repo; no
`kaggle_secrets` — `DATABASE_URL` arrives as a plain GitHub Secret; no
`params.json` — inputs arrive as env vars). Historical data still comes
from the existing Kaggle **Dataset** (Part E of `KAGGLE.md`, unchanged)
via a plain `kaggle datasets download` call in the workflow — that's
a simple file transfer with none of the kernel-execution opacity that
caused this switch, so there was no reason to move the data source too.
Chose this over further Kaggle-kernel debugging because the exact same
`requirements.txt` already installs and runs successfully on GitHub
Actions every ~5 minutes (`orchestra.yml`) — reusing a proven-reliable
log viewer and dependency install, not a new unknown. `_push_kaggle_kernel()`
is kept in `api/index.py`, unmodified, as a documented fallback (not
deleted) in case this new path hits its own blocker. Added `requests`
to `pyproject.toml`'s `dependencies` (used by the new dispatch call) —
per the standing rule in `CLAUDE.md` Section 7, the exact mistake that
caused the 2026-10-19 `kaggle`-dependency incident. See `KAGGLE.md`
Part J for the new setup steps (two new GitHub secrets —
`KAGGLE_USERNAME`/`KAGGLE_KEY`, reusing the existing Vercel values — and
one new Vercel env var, `GITHUB_DISPATCH_TOKEN`, reusing the same PAT
as the cron pinger) — **not yet set up or tested live**, since both are
manual steps outside what this session can do.

**Previous update, 2026-10-25, third pass same day.** Root cause of the
whole multi-day heartbeat/DB-identity mystery **confirmed by the
owner**: a Vercel-native **Neon** Postgres integration, attached early
in the project's life (before Supabase was chosen) and never removed,
was silently injecting its own `DATABASE_URL` into the Vercel
deployment — overriding the manually-set Supabase value with the same
name. Vercel's "healthy" Postgres connection was therefore reaching
Neon the whole time, while GitHub Actions wrote to Supabase — two
genuinely different, independently-healthy databases, exactly as
suspected, just not for a GitHub-Actions-secret reason. **Fixed by the
owner deleting the Neon integration in Vercel's dashboard.** Confirmed:
the Dashboard now shows real data. The `GET /api/health`/`db_ping()`/
cron-log identity-reporting work from the previous pass is what made
this diagnosable at all (comparing the two sides' `current_database()`)
— keeping it in place permanently as a standing diagnostic, not a
one-off. Lesson generalized into `VERCEL_DEPLOY.md` below: **any**
Vercel-native storage integration (Neon, Postgres, KV, Blob) can inject
env vars that silently outrank a manually-set one of the same name —
check Vercel's **Storage** tab for unexpected integrations whenever an
env var "isn't taking effect" and redeploying doesn't help.

Four more items from the same 2026-10-25 report, now addressed:
1. **TP/SL close-tracking bug, root cause found.** The simulated
   ("Legacy BTC/ETH manual") tracker that was supposed to detect TP/SL
   hits under `SIGNAL_ONLY_MODE` only works via plain Python globals
   (`btc_tracker`/`btc_ordered_signal`/etc.) — which reset to their
   defaults on every fresh `cron_runner.py` process. A signal generated
   in one hourly tick could only ever be closed by that SAME process's
   one-time price check; the next tick starts over with no memory of
   it. This explains both halves of the report: ~22 signals that could
   never close, and the 2 that did showing a nonsensical `+$3.00` PnL
   on an `SL HIT` (the old math was `current_bal - wallet_balance`,
   and `wallet_balance` is never actually fetched in `SIGNAL_ONLY_MODE`
   since Binance is geo-blocked, so it silently collapsed to
   `current_bal - 0`, a placeholder figure, independent of TP or SL).
   **Fixed**: added `_check_signal_only_closes()` to `pyq_p4.py` — a
   new, stateless check driven entirely by `pyquant_signals` (not
   in-memory globals), run every tick for every still-open `simulated`
   signal, scanning every 1-minute bar since the signal opened (not
   just the current tick's single bar — see item 4 below for why that
   matters) via a new `_fetch_bar_extremes_since()` helper. PnL is now
   `pyquant_signal_math.outcome_pct()` — a real leveraged price-return
   percentage, not a balance diff. Old rows closed before this fix keep
   their stale `+$3.00` `realized_pnl`; not backfilled (out of scope,
   and not trustworthy data to silently rewrite).
2. **PnL-by-percentage, implemented.** New nullable `realized_pnl_pct`
   column on `pyquant_signals` (Postgres `ADD COLUMN IF NOT EXISTS`,
   SQLite's try/except `ALTER` pattern), populated by
   `_check_signal_only_closes()` going forward. `GET /api/dashboard`'s
   headline PnL stat is now the AVERAGE leveraged % return across
   recently-closed signals that have one, falling back to the old
   summed-USD figure only when none do yet. `web/index.html` and
   `web/signals.html` prefer `realized_pnl_pct` (as `fmtPct()`) over
   `realized_pnl` wherever both might be present.
3. **Hide failed backtests, implemented.** `GET /api/runs` now takes
   `include_failed` (default `false`) and excludes `status == "failed"`
   rows unless explicitly asked — `web/backtest.html` already calls
   `/runs` with no extra params, so it gets the filtered view for free,
   no frontend change needed.
4. **GitHub Actions cron cadence, root cause found — this is the "every
   5-6 hours" report.** `gh run list` against the live workflow shows
   every recent `schedule`-triggered run completing successfully, but
   2-5 hours apart, not 5 minutes — `orchestra.yml`'s `cron: "*/5 * * *
   *"` is configured correctly; GitHub's own scheduler is simply not
   firing anywhere near that often (a known, documented GitHub Actions
   limitation under load, not something fixable from inside the
   workflow file). This is the direct cause of the "hasn't seen a tick
   in 104 minutes" report and a real contributor to item 1 above (long
   gaps between the only price-check each tick gets). **Fix documented**
   in `GITHUB_ACTIONS_SETUP.md` Part G: drive the existing
   `workflow_dispatch` trigger from a real external 5-minute cron
   service (e.g. cron-job.org) calling GitHub's REST dispatch endpoint
   with a repo-scoped PAT, instead of relying on GitHub's own unreliable
   `schedule:` event. **Needs the owner to actually set this up** (PAT +
   cron-job.org account are both manual, outside what Claude can do from
   here) — not yet done.

Kaggle 409 Conflict on `SaveKernel` is now **confirmed still occurring**
after the Part D doc rewrite — the owner corrected an earlier "504"
report back to the original 409. Since `kernel-metadata.json` was
already (and still is) `"kernel_type": "script"`, this almost certainly
means the mismatched kernel from before the doc fix was never actually
deleted on Kaggle's side yet (a 409 Conflict on `SaveKernel` is a
Kaggle-side fact about the kernel that already exists at that slug — no
further code change can fix it). **Action needed: go to
`https://www.kaggle.com/code/<username>/pyquant-backtest-runner`,
delete whatever kernel currently exists there (notebook or script),
and trigger one fresh backtest** — that first push creates a new,
correctly-typed script kernel from scratch (see `KAGGLE.md` Part D's
"Current setup" section, already in place). If it still 409s after an
actually-confirmed deletion, that would point to something new (e.g. a
caching delay on Kaggle's side) worth re-investigating.

**Previous update, 2026-10-25, second pass same day.** Two more
findings: (1) the Kaggle 409
Conflict's real cause was a self-inflicted kernel-type mismatch —
`KAGGLE.md`'s own setup instructions had the owner manually create a
**notebook**-type kernel, while `kernel-metadata.json` pushes
`"kernel_type": "script"`; Kaggle's kernel type is fixed at creation, so
every automated push conflicted with the manually-created kernel. Fixed
by rewriting Part D to not manually create anything — a script kernel
is created correctly by the first automated push itself. (2) After the
owner pasted the same `DATABASE_URL` into both Vercel and GitHub and
still saw no heartbeat, decided the standalone identity script
(`scripts/check_db_identity.py`) was too much friction to rely on as
the primary tool — moved the same ground-truth check (`current_database()`/
`inet_server_addr()`/`inet_server_port()`) directly into the product:
`db_ping()` now returns it, `GET /api/health` surfaces it from Vercel's
side, and `cron_runner.py` now prints AND persists it from the engine's
side on every tick — so comparing "is this the same database" is just
reading two JSON responses / one Actions log line, no local script
required (the script stays as a heavier fallback for when the engine
side has never written anything at all). See the Active section below
for what to check next, and the Decisions log for why Supabase Storage/
S3 was ruled out as a fix for this.

**Previous update, 2026-10-24, second pass same day.** Confirmed good news
first: the 2026-10-24 13:30 UTC GitHub Actions run actually fired a real
BTC signal end to end (`"BTC/USD signal: {...}"` →
`"[SIGNAL-ONLY] BTCUSDT SELL Limit @ 80739.0 — simulated entry"`) — the
engine genuinely works. `GET /api/health` (added earlier today) also
confirms Vercel's own Postgres connection is healthy right now. Yet
`GET /api/heartbeat` still shows nothing, even though `cron_runner.py`'s
heartbeat write has been live for days and that same run completed with
"Tick complete, no uncaught errors." Two independently-healthy Postgres
connections that never see each other's writes is the signature of the
GitHub Actions `DATABASE_URL` secret and Vercel's `DATABASE_URL` env var
pointing at two **different** databases — added
`scripts/check_db_identity.py` to prove this definitively (writes a
marker through one connection string, reads it back through the other)
without running any part of the actual trading engine locally. Also
fixed a second, unrelated bug found in the same batch of live attempts:
`KAGGLE_DATASET_SLUG` had been set to the full dataset page URL instead
of the bare `owner/slug` Kaggle's API expects — `create_backtest()` now
defensively strips a pasted URL down to the bare slug.

This is the living, checklist-level tracker — update it in the same
turn as any change that finishes an item, starts a new one, or makes a
decision worth remembering. `CLAUDE.md`/`DOCS.md` explain the *why* and
the technical detail behind each item in prose; this file is the
at-a-glance *status* — what's done, what's active, what's next, and
what was decided along the way. When you finish something, check it off
here before moving on, even if you also wrote it up elsewhere.

## How to use this file (read this before editing it)

- Check an item's box (`[x]`) only when it is **deployed/pushed**, not
  merely code-complete — "built, not yet pushed" items stay unchecked
  with a note, since `CLAUDE.md`/`DOCS.md`/`BINANCE_GEOBLOCK.md` already
  track the code-complete-vs-pushed distinction in detail; this file's
  checkbox answers one question only: is it live.
- Add new items under "Active" as soon as they're identified, even
  before any code exists for them — an open question or a newly-found
  bug belongs here immediately, not after it's fixed.
- Move a finished "Active" item down to "Done" with the date; don't
  delete the line.
- The "Decisions" log is append-only — never edit a past entry to make
  it sound like better foresight than existed at the time. If a
  decision is later reversed, add a new entry that references the old
  one.

## Phase status

| Phase | Scope | Status |
| --- | --- | --- |
| Phase 0 | Unify signal/SL/TP math (`pyquant_signal_math.py`) | ✅ Done, deployed |
| Phase 1 | Externalize state, resilience, capital-base fixes | ✅ Done, deployed |
| Phase 2 | UI + persistence (Vercel/FastAPI, Supabase, 5 web pages) | ✅ Done, deployed |
| Phase 2.5 | Orchestra hosting migration: VPS → GitHub Actions cron | ✅ Live, 2 reliability bugs found and fixed after the fact (see below) |
| Phase 3 | UI-completion: backtest-from-UI (Kaggle), reliability hardening | 🔶 In progress — see Active below |
| Phase 4 | Real order placement from a non-geo-blocked host; further UI-completion items (6.2 in `CLAUDE.md`); Vercel+QStash re-architecture (6.3) | ⬜ Not started |

## Active (Phase 3)

- [x] **Backtest "reform" pass (2026-10-27)** — signals baseline/"fresh
  start", run-detail modal, Kaggle manual-trigger rewrite +
  `POST /api/backtests/sync-to-kaggle`, Backtest-vs-Live Compare page.
  Full writeup at top of this file. Code-complete, pushed; "Sync to
  Kaggle" and Compare page not yet exercised against a real live run.
- [x] **Backtest POST 502, round 1 — Read-only $HOME.** Real error was
  `[Errno 30] Read-only file system: '/home/sbx_user1051'` — Vercel's
  sandboxed $HOME, which the `kaggle` package's `KaggleApi` still
  touches (resolving/creating its config dir) even with
  `KAGGLE_USERNAME`/`KAGGLE_KEY` set as env vars. Fixed in
  `_push_kaggle_kernel()`: point `KAGGLE_CONFIG_DIR` at `/tmp/.kaggle`
  and pre-write a `kaggle.json` there. **Confirmed fixed by the next
  live attempt** — it got past this point and hit a different error
  (below), proving this specific blocker is cleared.
- [x] **Backtest POST 502, round 2 — bad dataset slug.** Next live
  attempt (2026-10-24) got past the $HOME fix and failed with Kaggle's
  own `"Invalid dataset specification"` — `KAGGLE_DATASET_SLUG` had
  been set to the full dataset page URL
  (`https://www.kaggle.com/datasets/cliffordgambo/pyquant-historical-data`)
  instead of the bare `owner/slug` form the API expects. Fixed two
  ways: (a) config — set `KAGGLE_DATASET_SLUG` in Vercel to just
  `cliffordgambo/pyquant-historical-data`; (b) code —
  `create_backtest()` now strips a pasted URL down to the bare slug
  defensively, so this specific mistake can't recur even if the config
  is wrong again. **Code-complete, not yet pushed** — still needs a
  live retry past this point to see if the Kaggle kernel push actually
  succeeds (never yet reached).
- [ ] **Confirmed separately: a `POST /api/backtests` attempt also hit a
  plain 500 "state store write failed"** on an EARLIER try the same
  session, before the slug error — i.e. `request_backtest()`'s own
  Postgres write failed outright that one time. `GET /api/health`
  checked moments later showed Vercel's Postgres connection healthy
  (`{"ok":true,"db":{"ok":true,"backend":"postgres"}}`), so this reads
  as an intermittent failure (cold start / Supabase connection-pooler
  hiccup), not a standing misconfiguration — nothing further to fix
  unless it recurs. Noted, not actioned.
- [ ] **Kaggle 409 Conflict on SaveKernel — root cause found 2026-10-25,
  STILL OCCURRING as of the same day's later report.**
  kernel-type mismatch. `KAGGLE.md` Part D told the owner to click **New
  Notebook** (creates a notebook-type kernel); `kernel-metadata.json`
  pushes `"kernel_type": "script"`. Kaggle fixes a kernel's type at
  creation and never changes it on a later push, so every automated
  push conflicted with the manually-created notebook. Fixed: rewrote
  Part D so nothing is created manually — the first automated push
  creates a correctly-typed script kernel itself (and is expected to
  fail/never report back, since no `DATABASE_URL` secret is attached to
  a kernel that doesn't exist yet to attach it to); the owner deletes
  the old mismatched kernel once, then Part F's secret attachment
  happens on the freshly-created one. **Code/doc-complete and pushed to
  this delivery, but the 409 persisted on the next live attempt** — the
  owner's Kaggle dashboard still shows a Notebook at that slug, meaning
  the old mismatched kernel has not actually been deleted yet (this is
  a Kaggle-side fact, not something any further code change touches).
  **Next step is purely manual**: delete the kernel at
  `kaggle.com/code/<username>/pyquant-backtest-runner`, then trigger one
  fresh backtest from the UI to let the first push recreate it correctly
  typed.
  **2026-10-26 update: done, and it worked** — the owner deleted the old
  kernel and confirmed the next push created a correctly-typed script
  kernel. **But the backtest still failed**, with Kaggle's own UI
  showing no error log at all — see the top-of-file 2026-10-26 entry for
  the full writeup and the GitHub Actions alternative this led to. This
  item is superseded by the two new items directly below; not pursuing
  further Kaggle-kernel debugging unless the GitHub Actions path also
  hits a wall.
- [x] **GitHub Actions backtest-execution alternative, built 2026-10-26**
  — `.github/workflows/backtest.yml` + `kaggle_backtest/
  run_backtest_gha.py` + `api/index.py`'s `_trigger_github_backtest()`.
  See top-of-file entry for the full design. **Code-complete, pushed —
  NOT yet set up or tested live**: needs `KAGGLE_USERNAME`/`KAGGLE_KEY`
  added as GitHub repo secrets and `GITHUB_DISPATCH_TOKEN` added as a
  Vercel env var (both documented step-by-step in `KAGGLE.md` Part J),
  then a real "Run Backtest" click to confirm end to end.
- [ ] **Decide whether to keep both backtest paths long-term, or retire
  the Kaggle-kernel one.** Deliberately not deleting `_push_kaggle_kernel()`/
  `run_backtest.py`/`kernel-metadata.json` yet — they're correct code
  for a path that worked right up until an unexplained Kaggle-side
  failure, and keeping them costs nothing but a little dead weight.
  Revisit once the GitHub Actions path has a few confirmed successful
  runs under it.
- [ ] **Still open: `GET /api/heartbeat` was empty even after the owner
  pasted matching `DATABASE_URL` values into both Vercel and GitHub.**
  `GET /api/dashboard` DOES show real data written moments earlier by
  Vercel itself (run #13, the Kaggle-409 failure, with its real error
  text) — proving Vercel's own write-then-read round-trip works fine.
  Only the GitHub-Actions-written heartbeat key stays invisible. Still
  consistent with a DB-identity mismatch, but "pasted the same string in
  both places" not fixing it means either (a) Vercel wasn't redeployed
  after saving the new value (Vercel does NOT apply an env var change to
  already-built deployments without an explicit redeploy — easy to miss,
  called out in `KAGGLE.md` Part C step 3 but easy to skip on a routine
  value *update* rather than first-time *add*), (b) no GitHub Actions
  run has actually completed since the secret was updated, or (c) a
  genuine copy-paste mismatch (trailing whitespace/newline, a
  special character in the password encoded differently). Rather than
  ask the owner to run `scripts/check_db_identity.py` again (friction:
  needs `psycopg2-binary`, two real strings pasted on a command line),
  moved the same proof into the product itself: `db_ping()` now also
  returns `current_database()`/`inet_server_addr()`/`inet_server_port()`
  — exposed in `GET /api/health` (Vercel's identity) and now ALSO
  printed to the GitHub Actions log AND persisted into the heartbeat
  payload itself by `cron_runner.py` (the engine's identity), every
  tick. **Next step: push this, manually trigger the GitHub Actions
  workflow once (Actions tab → this workflow → "Run workflow"), then
  compare GET /api/health's `db` object against either the Action's own
  log line (`"[cron_runner] DB identity this tick: ..."`) or, once a
  tick succeeds, `GET /api/heartbeat`'s `cron.db_identity` field — if
  `database`/`server_addr` differ, that's the mismatch, named and fixed
  directly rather than inferred.**

- [x] **Switch `DATABASE_URL` to Supabase's Transaction pooler string** —
  confirmed fixed via the 2026-10-19 22:55 UTC live cron log: no
  "Network is unreachable" errors, UKF state restored 18/18 models, and
  `daily_optimizer()` correctly skipped ("already ran today") instead of
  firing every 5 minutes. DB persistence across GitHub Actions ticks is
  now genuinely confirmed working, not just applied.
- [ ] **"No signal logged in hours, UI shows nothing but API-key
  presence" — diagnosed, partly addressed 2026-10-20.** Root cause is
  NOT a signal-generation bug: `generate_signal()`'s math is byte-for-
  byte the same thresholds as the validated `walkforward.py` (confirmed
  by re-reading both side by side), so a quiet stretch with zero fired
  signals is expected strategy behavior at current BTC/ETH volatility,
  not a stuck engine. The real gap was that the UI had **no way to tell
  "quiet but alive" apart from "not running at all"** — nothing was
  persisted on an hour where no signal fired. Fixed: `cron_runner.py`
  now writes a plain tick-heartbeat every ~5 min regardless of outcome;
  `pyq_p4.py`'s `hourly_task()` now persists a full per-symbol signal
  diagnostic (trend, pct_diff/vol vs. the volatility gate, the entry
  "strict" gate and how close price came to clearing it) every hour
  regardless of outcome; both are exposed via a new `GET /api/heartbeat`
  and shown on `web/live-monitor.html` (and a thin summary on the
  Dashboard). **Code-complete, not yet pushed** — needs a live tick to
  confirm the panel actually populates. Also found and fixed, in the
  same pass: `pyq_p4.py`'s `hourly_task()` never reset `my_signal` to
  `None` at the top of each per-symbol loop iteration, so a stale signal
  object from one symbol could in principle leak into the other
  symbol's `if my_signal:` check after an exception — latent, not yet
  observed live, fixed proactively.
- [ ] **If, after the heartbeat panel confirms the engine is genuinely
  ticking and still no signal fires for a long stretch (owner's call on
  "long"), consider a temporary, clearly-labeled relaxation of
  `strict`/`stricts` via `optimal_params.json`/DB-stored params** — NOT
  a code change to `generate_signal()` itself (that would silently
  diverge live behavior from the validated walkforward math — see the
  standing rule in Decisions below). This is deliberately left as a
  judgment call for the owner, not pre-emptively done, now that the
  diagnostic panel gives real numbers to decide from instead of guessing.
- [x] **Fix the Kaggle-backtest 502** — `kaggle` was listed only in
  `api/requirements.txt`, which Vercel's framework-preset deploy doesn't
  read; added to `pyproject.toml`. Code-complete, not yet pushed.
- [ ] **Push the pending zip** — UKF per-tick retraining fix, Kaggle
  backtest-from-UI integration, Binance geo-block fix (rounds 1 **and**
  2), the `pyproject.toml`/`api/requirements.txt`/`SUPABASE_SETUP.md`/
  `VERCEL_DEPLOY.md` fixes, and all planning docs. Blocked on manual
  apply+push (Claude's GitHub App has no push access — standing issue,
  see Decisions below).
- [ ] **Confirm both Binance fix rounds and the UKF fix actually resolve
  the live outage** — verified by unit test/code review, not yet by a
  live run with persistence also working (the Supabase outage above
  means no live run so far has had working state-store access to fully
  confirm the UKF fix's "no persisted state only on first run" claim).
  Re-check Actions run history after both the DB and Kaggle-dep fixes
  are pushed together.
- [ ] **One-time Kaggle setup** (account token, kernel, dataset, secret
  — `KAGGLE.md`) — not yet done on the Kaggle side; needed in addition
  to the dependency fix above before `POST /api/backtests` fully works.
- [ ] **Decide the real-trading hosting split** — residential/home IP
  vs. any datacenter VPS, per the Binance geo-block finding (see
  Decisions, 2026-10-18). Not urgent while `SIGNAL_ONLY_MODE=true`, but
  blocks ever turning it off.
- [x] **DB-identity mystery, finally resolved 2026-10-25** — a
  Vercel-native Neon integration was silently overriding the manually-
  set Supabase `DATABASE_URL`. Owner deleted the Neon integration;
  dashboard now shows real data. See the top-of-file entry for the full
  writeup; `VERCEL_DEPLOY.md` updated with a general warning about
  Vercel storage integrations.
- [x] **TP/SL simulated-close tracking bug, fixed** —
  `_check_signal_only_closes()` (new, in `pyq_p4.py`) replaces reliance
  on in-memory globals with a DB-driven check against every open
  `simulated` signal, every tick, scanning the full bar range since
  each signal opened. **Code-complete, not yet pushed** — needs a live
  tick (or several, given the cron-cadence finding below) to confirm
  signals actually start closing with sane PnL.
- [x] **PnL-by-percentage, implemented** — new `realized_pnl_pct`
  column, populated going forward; Dashboard/Signals pages prefer it
  over the USD figure. **Code-complete, not yet pushed.**
- [x] **Hide failed backtests from the runs list, implemented** —
  `GET /api/runs` excludes `status == "failed"` by default
  (`include_failed=true` to see them). **Code-complete, not yet
  pushed.**
- [ ] **GitHub Actions real cron cadence is ~2-5 hours, not 5 minutes**
  — confirmed via `gh run list` (see top-of-file entry). Fix documented
  in `GITHUB_ACTIONS_SETUP.md` Part G (external 5-minute pinger via
  `workflow_dispatch` + a repo-scoped PAT, since GitHub's own
  `schedule:` trigger can't be made more reliable from inside the
  workflow). **Purely a manual setup step for the owner** — no code
  change possible here.

## Backlog (Phase 4+, not yet started, no commitment to order)

- [ ] **Next.js migration (architecture decision to observe from
  2026-10-28 onward).** The owner has flagged this as the eventual fix
  for a class of bug this static-HTML-per-page frontend keeps
  producing: the nav bar (and anything else "shared") is hand-copy-pasted
  into every `web/*.html` file, so adding a page (Compare) or changing
  the nav means editing N files identically, and any ONE of them missed
  or desynced is a real, reported bug (not a hypothetical) — see the
  "Compare menu only on Settings/Compare pages" report, 2026-10-27/28
  (which turned out to be a stale browser cache that turn, not a real
  desync, but the underlying architectural risk is real and will recur).
  Decided: **not worth fixing with more copy-paste discipline now** —
  every new page/shared-UI change from this point should be built
  assuming a future Next.js (or similar component-based) migration will
  absorb this class of bug for free via a shared layout component, so:
  avoid inventing further hand-rolled "sync nav across N files" tooling;
  keep each page's own JS/markup as decoupled from the others as
  practical (already mostly true — `app.js` holds the genuinely shared
  logic); and treat any new recurring cross-page duplication as a signal
  to prioritize the migration sooner rather than papering over it again.
  Not started — no scoping/timeline decided yet, this entry exists so
  that decision is tracked, not lost between sessions.
- [ ] Trigger `daily_optimizer()` on demand from the UI (same
  queued-job pattern as the Kaggle backtest integration).
- [ ] Editable (non-secret) settings from the UI — `/api/config` is
  currently read-only.
- [ ] Per-symbol pause (finer-grained than the global `trading_enabled`
  switch).
- [ ] Vercel+QStash re-architecture (`CLAUDE.md` Section 6.3) —
  eliminates GitHub Actions' 5-minute cron floor, at the cost of
  revisiting the API's no-credentials trust boundary (`SPEC.md` Section
  4). Deferred until the GitHub Actions path is confirmed stable.
- [ ] `last_resort_stop_loss_check()`'s close path doesn't log a Phase 2
  signal close-out — narrow fallback path, low priority.
- [ ] Repo housekeeping: `*.log` files are committed and growing; add
  to `.gitignore` and `git rm --cached` next time `.gitignore` is
  touched anyway.
- [ ] Validate the GitHub Actions UKF-persistence/restart-safety path
  via an actual walkforward comparison (this is the validation
  `SIGNAL_ONLY_MODE` was built to let the project proceed without
  waiting on).
- [ ] Automate the Kaggle dataset refresh (currently manual — `KAGGLE.md`
  Part I has 3 brainstormed options; leaning recommendation is adding a
  `dataset_create_version()` call to `daily_optimizer()`, reusing data
  it already downloads). Not yet designed in code-level detail.

## Done (newest first)

- **2026-10-20** — Signal-visibility audit (triggered by owner report:
  "no signal logged in hours, UI shows nothing but API-key confirmation",
  cron log attached). Confirmed the pooler fix is genuinely live (no DB
  errors, state restored 18/18 UKF models, `daily_optimizer()` correctly
  self-guarded). Confirmed `generate_signal()`'s thresholds are identical
  to `walkforward.py`'s — not a reduced/diverged live version. Added:
  `cron_runner.py` tick-heartbeat (every invocation), `pyq_p4.py`
  per-symbol signal diagnostics (every `hourly_task()` tick, fired or
  not), `GET /api/heartbeat`, a heartbeat summary on `/api/dashboard`,
  and a heartbeat + diagnostics panel on `web/live-monitor.html` (+ a
  small heartbeat card on `web/index.html`). Also fixed a latent bug
  found in the same pass: `hourly_task()` never reset `my_signal` to
  `None` per loop iteration. Code-complete, not yet pushed.
- **2026-10-19** — Found (via live cron + Vercel logs) and fixed two
  config bugs, neither requiring code changes to behave correctly once
  applied: Supabase `DATABASE_URL` was the IPv6-only Direct connection
  string (fix: `SUPABASE_SETUP.md` now specifies the Transaction pooler
  string); `kaggle` dependency was only in `api/requirements.txt`, which
  Vercel doesn't read (fix: added to `pyproject.toml`). Also condensed
  `CLAUDE.md` (650→~240 lines) and tightened `DOCS.md`/`SPEC.md` for
  token economy — no information removed, moved into cross-references.
- **2026-10-19** — Binance fix round 2: `PositionGuard._detect_hedge_mode()`
  was the next crash after round 1 (a live run found it); fixed, audited
  every other Binance call site in both files, verified with an
  isolated 4-case unit test. Code-complete, not yet pushed.
- **2026-10-18** — Binance geo-block fix round 1 (`_create_binance_client()`
  non-fatal under `SIGNAL_ONLY_MODE`) + full status/retrospective report
  (Claude Docs artifact) + `BINANCE_GEOBLOCK.md`. Code-complete, not yet
  pushed at the time; superseded by round 2 above.
- **2026-10-18** — UKF per-tick retraining fix (`make_ukf_shell()`) +
  Kaggle backtest-from-UI integration (`POST /api/backtests`,
  `kaggle_backtest/`, `web/backtest.html`, `KAGGLE.md`). Code-complete,
  not yet pushed.
- **2026-10-18 (pushed by owner)** — `SIGNAL_ONLY_MODE`: generate/log/
  track signals with zero exchange calls. Live on `origin/main`.
- *(earlier history: GitHub Actions cron migration, Phase 2 UI/API/
  Supabase, Phase 1 state externalization, Phase 0 math unification —
  see `CLAUDE.md` Section 5 for the full account; not re-listed here
  item-by-item since they predate this file.)*

## Decisions

- **2026-10-27** — Compare page ships as aggregate-only (v1), not
  per-trade. `walkforward.py`'s `run_walk_forward()` already builds an
  `all_trades` list internally but never persists it past the in-memory
  return value, so there's currently nothing on the backtest side to
  match individual live signals against. A true per-trade version needs
  (a) a new `pyquant_backtest_trades` table (run_id FK, symbol, side,
  entry/exit price+time, pnl_pct), populated from `all_trades` at save
  time the same way `pyquant_signals` already works for live trades,
  and (b) a day/week-bucketed matching strategy rather than 1:1 trade-id
  matching, since live ticks on a 5-minute real-time cadence while the
  backtest walks fixed historical windows — exact timestamps won't
  align. Deliberately deferred to a v2 rather than blocking this pass:
  it's a real schema addition, not a tweak. Full note also lives inline
  on `web/compare.html` itself.
- **2026-10-25** — Considered and rejected switching the shared state
  store to Supabase Storage (an S3-compatible object-storage bucket)
  when the DATABASE_URL-mismatch investigation made the owner ask
  whether "a Supabase S3 bucket" would be a surer fix. It would not be:
  object storage has no atomic read-modify-write primitive, which
  `try_claim_run()` (run guards), `log_signal()`/`update_signal_outcome()`,
  and `request_backtest()`/`update_run_status()` all depend on for
  correctness under concurrent/overlapping invocations — building that
  safely on top of S3-style storage means re-implementing what Postgres
  already gives for free (`INSERT ... ON CONFLICT`, row-level atomicity),
  for no benefit, while losing SQL filtering/ordering that `list_signals()`/
  `list_runs()` already rely on. The actual problem is operational (two
  independently-valid `DATABASE_URL` values that may not point at the
  same database, or a missed Vercel redeploy) — a config/deploy-process
  issue, not an architecture one. Postgres stays; see the Active section
  above for the real fix in progress (in-product DB-identity reporting).
- **2026-10-20** — When "no signal is firing" is reported, default to
  treating it as a visibility problem to diagnose (is the engine
  ticking? how close are the real numbers to threshold?) before treating
  it as a strategy problem to fix by loosening thresholds — loosening
  `strict`/`stricts` live would silently diverge from the validated
  `walkforward.py` reference, which the standing promotion-path rule
  (`INTENT.md` Section 5 / `SPEC.md` Section 2) exists specifically to
  prevent. The owner explicitly authorized reverting signal mechanics
  temporarily if needed to prove the pipeline; that authorization is
  intentionally not exercised yet, now that the diagnostic panel
  (`GET /api/heartbeat`) can show whether it's actually needed, with
  real numbers, instead of guessing blind.
- **2026-10-19** — Use Supabase's Transaction pooler connection string
  (port 6543, IPv4) for `DATABASE_URL` everywhere — GitHub Actions and
  Vercel are both now short-lived-invocation shapes (neither is the
  long-running VPS process the old Direct-vs-pooled split assumed), so
  one string suits both; no need to maintain two connection strings.
- **2026-10-19** — A dependency used only by `api/index.py` must be
  added to `pyproject.toml`'s `dependencies`, never only to
  `api/requirements.txt` (which Vercel's framework-preset deploy does
  not read) — `api/requirements.txt` is kept only as a human-readable
  manifest and local-dev convenience; every package in it must also be
  in `pyproject.toml` or it silently isn't installed.
- **2026-10-19** — `_detect_hedge_mode()` checks `SIGNAL_ONLY_MODE`
  directly rather than `self.binance is None`, because a geo-blocked
  but non-`None` client object fails every real call just as fatally —
  "is it `None`" was the wrong test; "do we even need a correct answer"
  is the right one. Documented in full in `BINANCE_GEOBLOCK.md` and
  `DOCS.md` Section 13.
- **2026-10-18** — Binance's compliance geo-block is treated as a
  structural property of cloud/datacenter IP ranges in general, not a
  GitHub-Actions-specific quirk — meaning most free-tier VPS options in
  `VPS_SETUP.md` are presumed exposed to the same block until proven
  otherwise. Recommendation: split hosting — signal-only generation
  stays on GitHub Actions (free, no working Binance connection needed);
  real order placement (paper or live) should move to a residential/
  home IP host. **Not yet acted on** — tracked as an Active item above.
- **2026-10-18** — Held a requested code fix (the Binance round-2 fix)
  for one turn at the owner's explicit request, to allow review/
  consolidation of everything delivered so far before continuing. No
  code was changed during that pause; only a status report was produced.
- **(standing, undated)** — Claude's GitHub App has no push access to
  `Pyrex611/pyq-p3`. Every code delivery from a Claude session ships as
  a zip of only new/modified files (never a whole-repo re-zip — explicit
  standing feedback, see `CLAUDE.md` Section 7) and is applied/pushed by
  the owner manually. Resolving this (installing the GitHub App, or
  reconnecting GitHub in claude.ai settings — both linked from
  `CLAUDE.md` Section 5) would remove this round-trip going forward.
- **(standing, undated)** — A strategy change must pass `walkforward.py`
  before paper, and paper before live (`INTENT.md` Section 5) — this is
  a risk-management rule, not a process formality, and should not be
  shortcut even under time pressure.

## Keep this file current

If you're closing out a turn that changed what's done, what's active,
or what was decided, and you haven't touched this file, that's a sign
something was missed — update it before finishing, the same way
`CLAUDE.md` Section 9 asks for its own Section 5/6/7.
