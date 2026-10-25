# PLAN.md — Roadmap, status, and decision log

**Last updated:** 2026-10-25. Two more findings: (1) the Kaggle 409
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
- [x] **Kaggle 409 Conflict on SaveKernel — root cause found 2026-10-25:**
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
  happens on the freshly-created one. **Code/doc-complete, not yet
  pushed** — needs the owner to delete the old kernel and run the
  create-then-attach-secret sequence once before the next real test.
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
