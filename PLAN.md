# PLAN.md — Roadmap, status, and decision log

**Last updated:** 2026-10-20 (signal-visibility audit: confirmed the
Supabase fix is live — state now persists across GitHub Actions ticks
with no DB errors; diagnosed "no signal logged in hours, UI shows
nothing" as the UI having no proof-of-engine-life rather than a signal
bug; added a heartbeat + per-symbol signal-diagnostic panel; fixed a
latent cross-symbol signal-leak bug found along the way).

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

- [ ] **Backtest POST still 502s after the Kaggle one-time setup +
  pyproject.toml fix were both confirmed deployed (origin/main has them;
  Vercel got a fresh deployment ID).** The real cause is currently
  unknown because the frontend was silently discarding the API's actual
  error `detail` on any non-2xx response — fixed 2026-10-20
  (`web/assets/app.js`'s `apiGet`/`apiPost` now surface it; added a
  catch-all FastAPI exception handler in `api/index.py` so even a raw,
  unhandled crash returns `{"detail": ..., "trace": ...}` instead of an
  empty 502 body). **Next step is simply to retry the backtest button and
  read the now-detailed error message** — no further guessing until that
  text is in hand.
- [ ] **Suspected (not yet confirmed) second DB-config issue: Vercel's own
  `DATABASE_URL` may not match the GitHub Actions secret.** Evidence: a
  direct `GET /api/runs` (no filter) on the live deployment returned
  `{"runs":[]}` — completely empty, not just the daily-optimizer type —
  while the engine side's cron log shows `daily_optimizer()` believing
  it already ran today. `pyquant_state.py`'s `get_json()`/`list_runs()`/
  `is_trading_enabled()` all silently swallow a DB error and return a
  safe-looking default (confirmed by reading the code: `is_trading_enabled()`
  defaults to `True` on **any** failure, which is why `/api/dashboard`
  showing `"trading_enabled": true` is NOT proof the DB read actually
  succeeded) — so "everything empty" and "DB totally unreachable from
  Vercel" look identical from the API's responses alone, and can't be
  told apart without actually reading the real exception. The GitHub
  Actions secret and Vercel's own environment variable are two
  *independent* places `DATABASE_URL` is set — it's easy to have updated
  one and not the other, or for one to have a stale value. Action:
  compare them character-for-character in the Vercel dashboard and the
  GitHub repo secret; once the error-detail fix above is deployed, a
  failing DB call directed through `/api/heartbeat` or `/api/dashboard`
  should also become loud (anything calling `_pg_conn()` for the first
  time on a bad connection string raises, which the engine's own
  try/except still swallows per standing rule, but the API's new
  catch-all handler won't hide if it surfaces from a route that doesn't
  already catch it itself — most routes here DO catch via `get_json()`'s
  own try/except, so this specific suspicion still needs a direct check
  of the two secrets, not just better error text).

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

## Backlog (Phase 4+, not yet started, no commitment to order)

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
