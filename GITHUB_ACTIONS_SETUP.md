# GitHub Actions Setup Guide — Cron-Based Orchestra (replaces the VPS, for now)

This replaces `VPS_SETUP.md` as the **current** path for running the
orchestra. `VPS_SETUP.md` is kept in the repo as a fallback — if GitHub
Actions' scheduling turns out to be too unreliable or too limited in
practice, that guide still works unchanged, and nothing about this
migration makes it harder to switch back.

**Read this first if you haven't already:** `CLAUDE.md` Section 6.3 and
this file's own "What changed and why" section below explain two real
trade-offs this migration accepts — coarser (5-minute, not 1-minute)
monitoring granularity, and a public repo. Both were discussed and
confirmed before this was built; this file is the "how," not the "why."

## What changed and why (short version)

- **GitHub Actions' cron trigger has a 5-minute floor** — there's no way
  to schedule it every 1 minute. `cron_runner.py` (new) runs once per
  invocation, calling every task function once; each one already
  self-guards against running more often than it should (via
  `pyquant_state.py`'s `try_claim_run()`), so `one_minute_task()` simply
  ends up running on a 5-minute cadence instead of 1-minute. This means
  up to ~5 minutes of added latency detecting a fill/TP/SL hit — **not**
  a reduction in actual risk protection, since the real TP/SL orders are
  Binance's own exchange-side bracket orders, enforced in real time
  regardless of how often this bot polls. What gets slower is this bot's
  own *awareness* (its logging, its Telegram notification, the UI's
  Signals/Live Monitor update), not Binance's enforcement.
- **This only stays free if the repo is public.** The dependency stack
  (numpy/pandas/scikit-learn/filterpy/optuna/alpaca/binance/telegram)
  takes real time to install; at ~288 runs/day (every 5 minutes), a
  private repo would very likely exceed GitHub's free 2,000 minutes/month
  and start being billed — plausibly more than the VPS this replaces. A
  public repo gets unlimited free Actions minutes. **Before pushing this,
  the repo needs its git history scrubbed of anything that shouldn't be
  public** (done as part of this delivery — see "What was purged" below)
  — actual credentials were never in the code, only in a gitignored
  `.env`, confirmed never committed.
- **The orchestra needs an actual `.env` file on disk, not just matching
  process environment variables.** `p4_utils.py` and `pyq_p4.py` both
  run a hard check — `if not _ENV_PATH.exists(): raise
  FileNotFoundError(...)` — **before** calling `load_dotenv()`. Setting
  the right values in the workflow's `env:` block is not enough on its
  own; the process still fails immediately because no literal `.env`
  file exists. The workflow's "Create .env file from secrets" step
  writes one from the repo's secrets before the run (this was caught
  and fixed directly in the workflow file, not in `pyq_p4.py`/
  `p4_utils.py` themselves — those files' behavior here is correct and
  intentional, just undocumented until now).
- **The UKF filters needed real code changes, not just a new host.**
  `pyq_p4.py` now persists each of the 18 Kalman filters' internal state
  (`.x`/`.P`) to the shared database after every tick and restores it at
  the top of every invocation — without this, a cron-restarted process
  would silently reset the filters' online learning every 5 minutes.
  Same reasoning for `btc_tracker`/`eth_tracker` and a handful of other
  in-memory flags (double-order prevention, which symbol has an open
  position, etc.) — all now round-trip through the database too.
  **This has not yet been validated against walkforward** — per the
  project's standing rule, don't trust `PYQUANT_MODE=paper`/`live` output
  from this cron-based path until a walkforward comparison confirms the
  restart-safe UKF behavior matches the original continuous-process
  behavior closely enough to trust.

## What was purged from git history (before going public)

Three categories of file were removed from **every commit**, not just
the current one (a public repo exposes full history, not just the
latest snapshot):
- `optimal_params.json`, `active_trades.json` — runtime state that
  reveals live strategy parameters / position detail, and is
  regenerated automatically anyway (the database is now the source of
  truth for both — see `DOCS.md` Section 3).
- `pyq_p3.log`, `pyq_p4.log` — large accumulated run logs with
  operational detail, already irrelevant now that `pyquant_state.py`'s
  tables are the real record.

Confirmed before doing this: no real API keys, tokens, or credentials
were ever committed to this repo at any point in its history (`.env`
was gitignored from the start) — this purge is about not publishing
*strategy/operational* detail, not about leaked secrets.

If you ever need the content of those files from before the purge,
it's still in whatever zip/backup you have from before this change —
the purge rewrites this repo's history, it doesn't touch any other copy
you're holding onto.

## Part A — Make the repository public

1. Go to `https://github.com/Pyrex611/pyq-p3`.
2. Click **Settings** (top nav bar of the repo page — requires owner
   access).
3. Scroll to the very bottom — **Danger Zone**.
4. Click **Change repository visibility**.
5. Select **Make public**, type the repo name (`Pyrex611/pyq-p3`) to
   confirm, and click **I understand, change repository visibility**.

Do this **after** the history-purge commit described above has already
been pushed (force-pushed, since it rewrites history) — not before. If
you're reading this and the push is still blocked by the GitHub App
authorization issue (see `CLAUDE.md` Section 5), resolve that first;
making the repo public with old, unpurged history still visible
defeats the purpose.

## Part B — Add repository secrets

1. On the repo page, click **Settings** → **Secrets and variables** (left
   sidebar) → **Actions**.
2. Click the green **New repository secret** button.
3. Add each of the following one at a time (**Name** exactly as shown,
   **Secret** = your actual value, then click **Add secret** — repeat
   for each row):

| Name | Value |
|---|---|
| `BINANCE_API_KEY` | Your Binance Futures mainnet API key |
| `BINANCE_SECRET_KEY` | Your Binance Futures mainnet secret key |
| `BINANCE_TESTNET_API_KEY` | Your Binance Futures **testnet** API key (only needed once you actually try `PYQUANT_MODE=paper`) |
| `BINANCE_TESTNET_SECRET_KEY` | Your Binance Futures testnet secret key |
| `BYBIT_API_KEY` | Your Bybit API key (hard-required by `_require_env()` in both `p4_utils.py` and `pyq_p4.py`, even though Bybit isn't the active execution exchange — the process raises `EnvironmentError` at startup without it) |
| `BYBIT_SECRET_KEY` | Your Bybit secret key (same reason as above) |
| `ALPACA_API_KEY` | Your Alpaca API key |
| `ALPACA_SECRET_KEY` | Your Alpaca secret key |
| `TELEGRAM_TOKEN` | Your Telegram bot token |
| `TELEGRAM_CHAT_ID` | Your Telegram chat id |
| `DATABASE_URL` | The same Supabase connection string from `SUPABASE_SETUP.md` / your Vercel project's env vars |

A secret's value is never shown again after saving (GitHub masks it) —
if you need to change one later, you re-add it with the same name; that
overwrites the old value.

## Part C — Add repository variables (`PYQUANT_MODE`, `SIGNAL_ONLY_MODE`)

These aren't secrets (one's the word "live"/"paper", the other is
"true"/"false"), so they use GitHub's separate **Variables** tab
instead:

1. Still under **Settings** → **Secrets and variables** → **Actions**,
   click the **Variables** tab (next to **Secrets**).
2. Click **New repository variable**.
3. **Name:** `PYQUANT_MODE`. **Value:** `live` (or `paper` — see
   `DOCS.md` Section 1 for what each means; given the standing
   "validate before trusting" note above, starting with `paper` is the
   safer choice until a walkforward comparison confirms the UKF
   persistence behaves correctly).
4. Click **Add variable**.
5. Click **New repository variable** again.
6. **Name:** `SIGNAL_ONLY_MODE`. **Value:** `true` to start — this is
   the new signal-generation-only mode (see `DOCS.md` Section 9): the
   engine generates real signals from live Alpaca data, logs them to
   `pyquant_signals` (`mode="simulated"`), and runs the existing
   per-minute tracker's simulated TP/SL check against real market
   prices — but makes **zero** Binance order-placement calls, live or
   testnet. Leave it `true` until you're ready to let the engine place
   real orders (at which point set it to `false` and rely on
   `PYQUANT_MODE`/`trading_enabled` as before — see that section for the
   precedence between all three).
7. Click **Add variable**.

If you don't add this variable at all, `cron_runner.py` defaults
`SIGNAL_ONLY_MODE` to `false` — i.e., full normal behavior gated only by
`PYQUANT_MODE` and the UI's trading on/off switch, same as before this
mode existed. Set it explicitly to `true` if the goal right now is
"watch it generate and log signals without risking anything."

## Part D — Confirm the workflow file is there and push everything

The workflow file (`.github/workflows/orchestra.yml`) and all the code
changes described above are included in this delivery's zip/commit.
Once GitHub push access is restored (see `CLAUDE.md` Section 5 — this is
the same blocker already reported, not a new one), push normally:

```
git push origin main
```

(The history-purge commit, if it hasn't been pushed yet either, needs
`git push --force origin main` instead, specifically because rewriting
history changes commit hashes — a normal push is rejected for a rewritten
branch. Force-pushing is safe here only because this is a single-person
repo with no other clones/collaborators relying on the old history.)

## Part E — Verify it's running

1. Go to the repo's **Actions** tab.
2. You should see **PyQuant Orchestra (cron)** listed as a workflow.
3. To test immediately without waiting for the next 5-minute mark, click
   it, then click the **Run workflow** dropdown (top right) → **Run
   workflow** button. This uses the `workflow_dispatch` trigger built
   into `orchestra.yml` for exactly this purpose.
4. Click into the new run (it appears at the top of the list within a
   few seconds) → click the **tick** job → watch the **Run one orchestra
   tick** step's live log output. You should see lines like
   `[cron_runner] Calling one_minute_task()…` for each task, and
   `[cron_runner] Tick complete, no uncaught errors.` at the end if
   everything succeeded.
5. If a task fails, its traceback prints directly in this same log —
   the run is also marked with a red ✕ in the Actions list so you don't
   have to open it to notice.
6. Check the UI (`https://pyq-p3.vercel.app/`) — the Live Monitor/
   Signals/Backtest pages should start showing real data once a few
   ticks have run (the Dashboard/signals come from the same database
   this workflow now writes to).

## Part F — What to watch for in normal operation

- **Occasional skipped or delayed ticks.** GitHub's own documentation
  states scheduled workflows are best-effort and can be delayed,
  especially for public repos and especially at the top of the hour
  when many repos' cron jobs fire at once. A skipped tick is not
  retried — the next scheduled one just picks up from wherever the
  persisted state left off.
- **A run that goes past 10 minutes gets killed** (this workflow's
  `timeout-minutes: 10`) — raise this in `orchestra.yml` if a tick
  genuinely needs longer (e.g. `daily_optimizer()`'s Bayesian search),
  but check the run's log first to understand why it was slow rather
  than just raising the limit blindly.
- **Actions usage**: Settings → Billing → Plans and usage (at the
  account level, not the repo level) shows Actions minutes used — on a
  public repo this should read effectively unlimited/free, so a nonzero
  but small paid balance there would be worth investigating (it would
  mean something is still being billed, which shouldn't happen on a
  public repo).

## Part G — Known limitation: real tick cadence is ~2-5 hours, not 5
## minutes (confirmed 2026-10-25)

**This is a platform limitation, not a bug in this repo's code.** Part
F above undersold it as "occasional skipped or delayed ticks" — the
real, confirmed behavior is much worse than occasional:

```
$ gh run list --workflow=orchestra.yml --limit 30 -R Pyrex611/pyq-p3
2026-10-25T12:05:06Z  (schedule)
2026-10-25T06:47:51Z  (schedule)   ← 5h17m gap
2026-10-25T01:36:55Z  (schedule)   ← 5h11m gap
2026-10-24T23:38:16Z  (schedule)   ← 1h59m gap
2026-10-24T21:33:40Z  (schedule)   ← 2h5m gap
2026-10-24T19:05:45Z  (schedule)   ← 2h28m gap
2026-10-24T16:51:34Z  (schedule)   ← 2h14m gap
2026-10-24T13:29:45Z  (schedule)   ← 3h22m gap
2026-10-24T09:28:36Z  (schedule)   ← 4h1m gap
2026-10-24T05:08:05Z  (schedule)   ← 4h20m gap
2026-10-24T00:51:01Z  (schedule)   ← 4h17m gap
```
Every single one of these runs completed successfully (`conclusion:
success`) — `orchestra.yml`'s `cron: "*/5 * * * *"` is configured
correctly and the code inside each tick works fine. GitHub is simply
not firing the `schedule` event anywhere near every 5 minutes. This
matches a widely-reported GitHub Actions limitation: GitHub explicitly
documents short-interval `schedule` crons as "best-effort" and
heavily throttles them under load — in practice this means something
closer to hourly-ish, not the configured 5 minutes, and there is no
account setting or workflow change that fixes this from inside
GitHub Actions itself. (`concurrency.cancel-in-progress: false` in
this workflow is unrelated to this — it only prevents two *successful*
triggers from overlapping; it isn't why triggers are being skipped.)

**Why this matters here specifically:** every piece of this project's
"5-minute" reasoning (`one_minute_task()`'s effective cadence, the
Live Monitor heartbeat freshness thresholds, how soon a TP/SL hit gets
detected under `SIGNAL_ONLY_MODE` — see `_check_signal_only_closes()`'s
2026-10-25 docstring in `pyq_p4.py`) assumed a few-minutes-at-most gap
between ticks. A 2-5 hour real gap is the direct cause of the Live
Monitor's "hasn't seen a tick in 104 minutes" report and a major
contributor to the TP/SL tracking bug (both from the 2026-10-25
session) — not because the code is wrong, but because the schedule
it's built around doesn't run anywhere near as often as designed.

### The fix: drive it from an external, real 5-minute scheduler instead

GitHub's own `schedule:` trigger is the unreliable part — so don't rely
on it. `workflow_dispatch: {}` is already in `orchestra.yml`, which
means the workflow can be triggered **on demand, via GitHub's REST
API**, by anything that can make an HTTPS request on an actual,
reliable timer. A free external cron service filling that role removes
GitHub's scheduler from the critical path entirely while reusing
everything else (the workflow, the secrets, `cron_runner.py`) unchanged.

1. **Create a fine-grained personal access token** (scoped narrowly —
   this repo only, one permission):
   - Go to `https://github.com/settings/personal-access-tokens/new`.
   - **Token name:** `pyq-p3-orchestra-dispatch`.
   - **Expiration:** pick the longest option offered (you'll need to
     regenerate and update the cron service when it expires).
   - **Repository access:** "Only select repositories" → choose
     `Pyrex611/pyq-p3`.
   - **Permissions** → **Repository permissions** → **Actions** → set
     to **Read and write** (this is the only permission needed; it's
     what lets the token trigger `workflow_dispatch`).
   - Click **Generate token** and copy it immediately (GitHub only
     shows it once).
2. **Create a free account at `https://cron-job.org`** (or any
   reliable external cron/uptime service that lets you set a custom
   HTTP method, headers, and a JSON body — EasyCron and cron-job.org
   both qualify; cron-job.org's free tier supports 1-minute intervals).
3. **Create a new cron job** in that service with these settings:
   - **URL:**
     `https://api.github.com/repos/Pyrex611/pyq-p3/actions/workflows/orchestra.yml/dispatches`
   - **Method:** `POST`
   - **Schedule:** every 5 minutes.
   - **Request headers:**
     - `Authorization: Bearer <the PAT from step 1>`
     - `Accept: application/vnd.github+json`
     - `Content-Type: application/json`
   - **Request body:** `{"ref": "main"}`
4. **Test it manually first** (most of these services have a "Run now"/
   "Test" button) and confirm in the repo's **Actions** tab that a new
   run of **PyQuant Orchestra (cron)** appears with event type
   `workflow_dispatch` right after.
5. Once confirmed, leave both the `schedule:` trigger (harmless — it
   can keep trying in the background, it just won't be the thing you're
   relying on) and the new external job in place. Actual cadence is now
   governed by cron-job.org's timer, not GitHub's.

This keeps the "only holder of credentials" trust boundary (CLAUDE.md
Section 2) completely intact — the external service never sees
`DATABASE_URL`/Binance/Alpaca secrets, only a repo-scoped token that
can do nothing but ask GitHub "please run this workflow now."
