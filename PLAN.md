# PLAN.md — Roadmap, status, and decision log

**Last updated:** 2026-10-19 (after the Binance fix round 2 + call-site
audit; not yet pushed).

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

- [ ] **Push the pending zip** — UKF per-tick retraining fix, Kaggle
  backtest-from-UI integration, Binance geo-block fix (rounds 1 **and**
  2), `BINANCE_GEOBLOCK.md`, this file + `INTENT.md`/`SPEC.md`. Blocked
  on manual apply+push (Claude's GitHub App has no push access to
  `Pyrex611/pyq-p3` — standing issue, see Decisions below).
- [ ] **Confirm the Binance fix (round 2) actually resolves the live
  outage** — round 1 alone did not (see Decisions, 2026-10-18); round 2
  is verified by an isolated unit test and code review but not yet
  against a real GitHub Actions run. Re-check the Actions run history
  within a few ticks of the next push.
- [ ] **Confirm the UKF per-tick retraining fix** the same way — also
  not yet verified live (`filterpy` doesn't build in the dev sandbox).
- [ ] **One-time Kaggle setup** (account token, kernel, dataset, secret
  — `KAGGLE.md`) — not yet done on the Kaggle side; needed before
  `POST /api/backtests` will actually work even after the zip is pushed.
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

## Done (newest first)

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
