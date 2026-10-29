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

## 6. What's next

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

## 7. Standing rules (apply without being re-asked)

- **Zip deliveries:** only new/modified files, full unabridged content,
  never a whole-repo re-zip (explicit standing feedback). Matters more
  now that the owner applies/pushes these by hand.
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
