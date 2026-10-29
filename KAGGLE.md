# Kaggle Backtest Integration — Setup Guide

This adds a "Run Backtest" button to the UI's Backtest page. Pressing it
triggers `walkforward.py`'s full UKF-training + 150-trial Optuna search
on **Kaggle**, not on this server — read the next section for why, if
you haven't already seen it in `CLAUDE.md` Section 6.1.

## Part A — Why Kaggle, and what it is/isn't for

**Is Vercel compute enough for this? No — confirmed, not a close call.**
Two independent, both-disqualifying reasons: (1) `walkforward.py`'s
UKF-training + 150-trial Optuna search runs for minutes; even Vercel
Pro's longest configurable serverless duration (800s) is tight-to-
insufficient, and Hobby's is far shorter. (2) It depends on local CSV
files (`btc_back366.csv`/`eth_back366.csv`) that need to already exist —
Vercel functions get only an ephemeral `/tmp`, wiped between
invocations, with no way to pre-populate it from a multi-MB historical
dataset on every cold start. No Vercel tier fixes either constraint for
this specific workload.

**Do we need Kaggle for the hourly/live signal-generation loop too? No.**
That loop (`hourly_task()`/`fifteen_minute_task()`/`five_minute_task()`/
`one_minute_task()`, running every 5 minutes via the GitHub Actions
cron workflow) does lightweight, incremental work per tick — one bar's
worth of data, a `.predict()`/`.update()` step on already-trained UKF
filters, a few conditionals in `generate_signal()`. No training, no
Optuna search. **This session also found and fixed a real bug** where
every tick was unnecessarily paying the full UKF-training cost anyway
(see `DOCS.md` Section 10) — with that fixed, the per-tick loop is cheap
and has run reliably on GitHub Actions' standard runner. Kaggle is only
for the heavy, on-demand, user-triggered workload: backtests/
walkforward runs from the UI. The daily optimizer (`daily_optimizer()`,
once per day, still does one real UKF retrain + a 150-trial search) is
a secondary candidate worth watching — see Part F.

**What this integration is (2026-10-25: EXECUTION moved to GitHub
Actions — see Part H):** the UI's "Run Backtest" button calls
`POST /api/backtests` on the Vercel API, which writes an immediate
`queued` row (so the UI has something to show right away) and triggers
the actual backtest run elsewhere. Historical data still comes from a
Kaggle **Dataset** either way (Part E below) — only WHERE the Python
code actually executes changed, from a Kaggle kernel to a GitHub Actions
job (Part H). Whichever path runs it writes its result straight back to
the same shared Supabase Postgres database the rest of the platform
already uses — the UI just keeps polling `GET /api/runs/{id}`, exactly
like any other run. **Do Part J, not just Parts B–G** — Parts B/C/E are
still needed (Kaggle account + dataset), but Part D/F's Kaggle-KERNEL
creation/secret-attachment steps are now only a fallback, not the active
path; skip straight to Part J after Part E unless you're deliberately
trying the Kaggle-kernel path again.

**What it is NOT:** a way to run backtests on a schedule, or to run two
at once. Only one backtest kernel exists (see Part D for why it's a
single, reused kernel slug) — triggering a second run while one is still
executing starts a new version of the same kernel; Kaggle's own queue
decides what happens to the one in flight. Fine for a single-operator
tool used occasionally; not something to build automation on top of as-is.

## Part B — One-time Kaggle account setup

You said you already have a Kaggle account you've used for backtests/
walkforwards — skip to whichever of these you haven't already done.

1. Go to `https://www.kaggle.com/settings` (sign in first if needed).
2. Scroll to the **API** section.
3. If you don't already have a token: click **Create New Token**. This
   downloads a file named `kaggle.json` containing your username and an
   API key — **save it somewhere private, it won't be shown again**
   (you can always create a new one later if you lose it, which
   invalidates the old one).
4. Open that `kaggle.json` file in a text editor. It looks like:
   ```json
   {"username":"yourkaggleusername","key":"a1b2c3d4e5f6..."}
   ```
   You'll need both values in Part C below.

## Part C — Add Vercel environment variables

1. Go to your Vercel project's dashboard (the one serving
   `https://pyq-p3.vercel.app`) → **Settings** → **Environment
   Variables**.
2. Add each of the following (**Key** exactly as shown, **Value** = your
   actual value, select all three environments — Production/Preview/
   Development — unless you only use one, then **Save**):

| Key | Value |
|---|---|
| `KAGGLE_USERNAME` | The `username` field from your `kaggle.json` |
| `KAGGLE_KEY` | The `key` field from your `kaggle.json` |
| `KAGGLE_DATASET_SLUG` | `yourkaggleusername/pyquant-historical-data` — the **bare** `owner/slug` form only, NOT the full dataset page URL (`https://www.kaggle.com/datasets/owner/slug`). Pasting the URL by mistake is exactly the error a live run hit on 2026-10-24: Kaggle's API rejected it with "Invalid dataset specification." `api/index.py`'s `sync_backtest_to_kaggle()` now defensively strips a pasted URL down to the bare slug, but set it correctly here anyway. (create this dataset in Part E below first, or come back and set this after) — **2026-10-29: only used by the Kaggle-kernel BACKUP path (Part K) now; the primary GitHub Actions path (Part J) pulls fresh data straight from Alpaca and doesn't read this.** |

3. Redeploy the Vercel project (**Deployments** tab → the three-dot menu
   on the latest deployment → **Redeploy**) so the new env vars take
   effect — Vercel doesn't pick up new environment variables on already-
   running deployments.

## Part D — Create the Kaggle kernel (fallback path — see Part J)

### ⚠️ 2026-10-25 incident: kernel type mismatch → 409 Conflict, THEN a
### second, undiagnosable failure after that fix — this is why Part J exists

After fixing the 409 below, a subsequent real run failed AGAIN —
Kaggle's own UI just showed "Failed" with no error log, no traceback,
nothing to act on. Nothing on this end could inspect it further (no
programmatic access to a Kaggle kernel's execution log from this
session). Rather than keep guessing at an opaque execution environment,
**Part J moves backtest EXECUTION to GitHub Actions instead** — same
historical data (this Kaggle dataset, downloaded directly, no kernel
involved), but running inside a job whose logs have already proven
reliable for this exact dependency stack. This Part D is kept as a
documented fallback, not deleted, in case the Kaggle-kernel path is
ever worth revisiting.

### ⚠️ 2026-10-25 incident (earlier the same day): kernel type mismatch → 409 Conflict

A live run hit `502 — Could not start the Kaggle run: 409 Client Error:
Conflict for url: .../SaveKernel`. Root cause: this doc used to tell you
to click **New Notebook** in step 1 below, which creates a
**notebook**-type kernel — but `kaggle_backtest/kernel-metadata.json`
pushes with `"kernel_type": "script"` (correct, since `run_backtest.py`
is a plain `.py` file, not a `.ipynb`). Kaggle's kernel type is set at
creation and can't be changed by a later push, so every automated push
to that already-existing notebook conflicted. The two sides (the doc's
manual step and the code's push) simply disagreed with each other.

**Fix — if you already created a kernel at this slug per the old
instructions, do this once:**
1. Go to `https://www.kaggle.com/code/yourkaggleusername/pyquant-backtest-runner`.
2. Open its **"..."** menu (or **File** → **Delete**, depending on
   Kaggle's current UI) and delete it.
3. Don't manually recreate it — step 1 below now explains why.

### Current setup (no manual kernel creation needed)

Unlike a notebook, a **script**-type kernel does not need to exist
before the API's first push — `kernel_type: "script"` in
`kernel-metadata.json` is enough for Kaggle to create it fresh, correctly
typed, the first time `POST /api/backtests` pushes to that slug. So:

1. Make sure Parts B/C/E below are done (Kaggle account token, Vercel
   env vars, the historical-data dataset) — then just trigger one
   backtest from the UI (Part G). That first push creates the kernel.
2. **This first run is expected to fail** (or never report back) — no
   `DATABASE_URL` secret is attached to it yet, so `run_backtest.py`
   can't call back `update_run_status()`. That's fine; its only job here
   is to make the kernel exist, correctly typed, at the right slug.
3. Once it exists, go straight to Part F to attach the secret, then
   trigger a second backtest — that one should complete and report back
   normally.
4. Confirm the slug matches: `https://www.kaggle.com/code/yourkaggleusername/pyquant-backtest-runner`.
   If Kaggle auto-adjusted it (e.g. appended `-runner2` because the slug
   was taken), use that exact slug in Part F and in
   `kernel-metadata.json`'s `"id"` field instead, then re-deliver that file.

## Part E — Create the historical-data Kaggle Dataset

`walkforward.py` needs `btc_back366.csv`/`eth_back366.csv` to exist —
these are gitignored (not in the repo) since they're large, operator-
maintained data files, the same ones you've already been using for your
own Kaggle/Colab backtest runs.

1. Go to `https://www.kaggle.com/datasets`, click **New Dataset**.
2. Drag in (or click to browse for) your current `btc_back366.csv` and
   `eth_back366.csv` files — whatever you've been using already.
3. **Title**: `PyQuant Historical Data` (or anything you like).
4. Before clicking **Create**, note the **URL slug** shown under the
   title field (usually auto-generated from the title, e.g.
   `yourkaggleusername/pyquant-historical-data`) — this is the value for
   `KAGGLE_DATASET_SLUG` in Part C.
5. Click **Create**.
6. **Keeping it current**: `walkforward.py`'s walk-forward window looks
   at the most recent days of data, so this dataset needs periodic
   refreshing (however often you've been refreshing it for your own
   manual runs already — daily or weekly is typical). To update it:
   go to the dataset's page → **Settings** (gear icon) → **New Version**
   → upload the refreshed CSVs → **Create Version**. No code changes
   needed on this end; the kernel always pulls whatever the dataset's
   current version is.

## Part F — Attach the DATABASE_URL secret to the kernel

1. Open your kernel from Part D:
   `https://www.kaggle.com/code/yourkaggleusername/pyquant-backtest-runner/edit`
2. In the editor, click **Add-ons** (top menu) → **Secrets**.
3. Click **Add a new secret**.
4. **Label**: `DATABASE_URL`. **Value**: the exact same Supabase
   connection string from `SUPABASE_SETUP.md` / your Vercel project's
   `DATABASE_URL` env var.
5. Toggle **Attached** to ON for this secret, for this notebook.
6. Click **Save**.

This only needs doing once — it survives every future version the API
pushes to this same kernel slug.

## Part G — Test it (fallback Kaggle-kernel path — see Part J for the active path)

1. Open the deployed UI's Backtest page
   (`https://pyq-p3.vercel.app/backtest.html`).
2. Leave the form fields blank (defaults to `walkforward.py`'s own
   module-level defaults — 15 days, 150 Bayesian trials, 10x leverage,
   Bayesian + ATR-adaptive SL both on) or fill in overrides, then click
   **Run Backtest**.
3. You should see "Run #N queued — Kaggle is starting it now." within a
   couple of seconds. If instead you see "Kaggle isn't configured on
   this deployment," go back to Part C — one of the three env vars is
   missing or the project wasn't redeployed after adding them.
4. The page polls every 15 seconds. Kaggle's own queue (not something
   this integration controls) typically takes a few minutes to actually
   start the kernel — "queued" can sit for a while before flipping to
   "running," that's normal.
5. To watch it directly: `https://www.kaggle.com/code/yourkaggleusername/pyquant-backtest-runner`
   → the latest version's log output shows exactly what
   `run_backtest.py` is doing (clone, pip install, copy dataset, run the
   backtest).
6. On completion, the page shows "Run #N completed." and the run appears
   in the "Walkforward Runs" table below, exactly like a manually-run
   `python3 walkforward.py` would.
7. **If it fails**: the page shows "Run #N failed: `<error>`" — the error
   text comes from whatever exception `run_backtest.py` hit (missing
   dataset file, a walkforward.py exception, etc.), and the kernel's own
   log (step 5) has the full traceback.

## Part H — Known limitations (accepted scope, not bugs)

- **One backtest at a time.** Triggering a second run while one is still
  executing pushes a new version of the same kernel — Kaggle's own
  behavior in that situation (queue, cancel-and-replace, or run both)
  isn't something this integration controls or has been tested against.
  Wait for one to finish before starting another.
- **No automatic dataset refresh.** The historical CSVs are refreshed
  exactly as often as you update the Kaggle Dataset in Part E — nothing
  here does that for you. If a backtest's results look stale, check the
  dataset's version history.
- **No retry on a failed push.** If `POST /api/backtests` itself fails
  (Kaggle API error, bad credentials), the queued row is marked
  "failed" immediately with that error — press the button again after
  fixing whatever Part G's troubleshooting step points to.

## Part I — Brainstorm: refreshing the Kaggle dataset from the platform itself

Raised 2026-10-19: can "No automatic dataset refresh" (Part H) be
automated, rather than a manual CSV upload every time? Three options,
roughly cheapest-to-build to most capable:

1. **Add a dataset-refresh step to `daily_optimizer()`.** It already
   does `data_download.py`'s full historical refresh once a day
   (`btc_back366.csv`/`eth_back366.csv` get written fresh on whatever
   host runs it — currently GitHub Actions). The Kaggle API's
   `dataset_create_version()` call is the same shape as
   `kernels_push()` (already used by `_push_kaggle_kernel()`) — point
   it at a folder containing the two refreshed CSVs and a
   `dataset-metadata.json` naming the existing dataset slug. This is a
   small, contained addition: a new `_push_kaggle_dataset_version()`
   helper near `_push_kaggle_kernel()` in `api/index.py`, OR directly in
   `p4_orchestra.py`'s `daily_optimizer()` if the orchestra host should
   own it instead (it already has the fresh CSVs on disk, avoiding a
   round trip through the API). Needs `KAGGLE_USERNAME`/`KAGGLE_KEY` as
   secrets wherever it runs — already a Vercel env var; would need
   adding as a GitHub Actions secret too if done from the orchestra side.
2. **Trigger it from the UI instead of automatically** — a "Refresh
   Kaggle dataset" button next to "Run Backtest" on `backtest.html`,
   calling a new `POST /api/kaggle-dataset/refresh` that does the same
   `dataset_create_version()` call on demand, using whatever CSVs the
   Vercel function has access to (which, per `SPEC.md`'s "no durable
   filesystem" constraint, means it would need to re-download fresh data
   itself via `data_download.py`'s logic first — more self-contained
   than option 1, but duplicates the daily download the orchestra
   already does).
3. **Skip the Kaggle Dataset mechanism entirely** — have
   `run_backtest.py` (the kernel itself) download fresh data directly
   from Alpaca at the start of each run, the same way `data_download.py`
   already does, instead of reading from an attached Dataset at all.
   Removes Part E's one-time Dataset setup and the "stale until
   manually refreshed" problem completely, at the cost of needing
   Alpaca credentials available to the Kaggle kernel (a new Kaggle
   Secret, `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` — a real scope increase
   for a kernel that currently only needs `DATABASE_URL`) and adding
   Alpaca API latency to every backtest run instead of just once a day.

## Part J — GitHub Actions alternative (2026-10-25, now the primary path)

Why this exists: see Part D's 2026-10-25 incident note. A backtest that
still failed after the kernel-type fix, with Kaggle's own UI showing no
error detail at all, made the Kaggle-kernel path undebuggable from this
session — so execution moved to GitHub Actions instead, reusing the
exact `requirements.txt` this repo's `orchestra.yml` cron workflow
already installs successfully on every tick (proven reliable) and
GitHub's own log viewer (also already proven reliable).

**2026-10-29 update: historical data no longer comes from the Kaggle
Dataset.** It originally did (a plain `kaggle datasets download` call —
no kernel involved, so none of the opacity that caused the original
switch applied to it), but that dataset only updates when someone
manually re-uploads a new version (Part H's "No automatic dataset
refresh" limitation) — running the UI's "check current behavior"
button against however-stale that snapshot happened to be defeated the
point, now that this is the PRIMARY path (confirmed working live). The
workflow now calls `data_download.py` directly against Alpaca instead —
the exact same call `p4_orchestra.py`'s `daily_optimizer()` already
makes once a day for the live engine, so the backtest and the live
engine's own UKF retraining now draw from the same kind of
always-fresh pull. No Kaggle Dataset maintenance is needed for this
path anymore at all; Part E's dataset is now only read by Part K's
Kaggle-kernel BACKUP path (below), since that script runs inside
Kaggle's own sandbox with no Alpaca credentials attached to it.

New files: `.github/workflows/backtest.yml` (the workflow) and
`kaggle_backtest/run_backtest_gha.py` (the script it runs — a GitHub
Actions counterpart to `run_backtest.py`, see that file's own docstring
for the exact differences). `api/index.py`'s `POST /api/backtests` now
calls `_trigger_github_backtest()` instead of `_push_kaggle_kernel()`.

### J1 — No new GitHub repository secrets needed

`ALPACA_API_KEY`/`ALPACA_SECRET_KEY` are already repo secrets from
`GITHUB_ACTIONS_SETUP.md` Part B (`orchestra.yml`'s own cron tick uses
them) — `backtest.yml` reuses the same two, nothing new to add. Same
for `DATABASE_URL`. (`KAGGLE_USERNAME`/`KAGGLE_KEY` as GitHub repo
secrets are no longer needed for this workflow — only Part K's Kaggle
backup path reads Kaggle credentials, and it reads them as Vercel env
vars, not GitHub repo secrets, since `_push_kaggle_kernel()` runs
inside `api/index.py` on Vercel, not inside a GitHub Actions job.)

### J2 — Add a Vercel environment variable: `GITHUB_DISPATCH_TOKEN`

This is the same **kind** of token as `GITHUB_ACTIONS_SETUP.md` Part
G's cron-reliability pinger (a fine-grained PAT, this repo only,
**Actions: Read and write** permission) — if you already created that
one, reuse its value here rather than making a second token:

1. Vercel project → **Settings** → **Environment Variables**.
2. **Key:** `GITHUB_DISPATCH_TOKEN`. **Value:** the PAT from
   `GITHUB_ACTIONS_SETUP.md` Part G step 1 (or a new one made the same
   way, if you'd rather keep them separate).
3. Select all three environments, **Save**, then **Redeploy** (Vercel
   doesn't apply a new env var to an already-built deployment —
   `VERCEL_DEPLOY.md`'s standing note).

### J3 — Test it

1. Open the deployed UI's Backtest page and click **Run Backtest**
   exactly as in Part G above — the button and the UI's polling behavior
   are unchanged; only what happens behind `POST /api/backtests` is
   different now.
2. To watch it directly: repo's **Actions** tab → **PyQuant Backtest
   (triggered by UI)** → the newest run → **backtest** job → step logs.
   Every step (`Download fresh historical data from Alpaca`, `Run
   backtest`) shows its own full output, and `run_backtest_gha.py`
   prints a step-by-step `[run_backtest_gha] ...` trace specifically so
   a failure anywhere is visible here — unlike the Kaggle-kernel path's
   blank failure.
3. **If `POST /api/backtests` itself returns "Backtest triggering isn't
   configured on this deployment"**: `GITHUB_DISPATCH_TOKEN` is missing
   on Vercel — check J2.
4. **If the GitHub Actions run itself shows a red ✕**: open its log
   (step 2 above) — every failure mode prints a real message, including
   ones that would have been silent on Kaggle (a missing/invalid
   `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`, the Alpaca download failing, a
   `walkforward.py` exception).

## Part K — Manual-trigger workflow (2026-10-26)

Why this exists alongside Part J: the owner asked for a path where the
walkforward script's values can be edited and the run started "from the
Kaggle UI" directly, with full visible console output — a different,
more observable execution context than either the opaque Kaggle-kernel
auto-run (Part D/G, still a documented fallback) or GitHub Actions
(Part J, the default "Run Backtest" button's path). This is that path.

### K1 — What changed

- `kaggle_backtest/run_backtest.py` was rewritten: it no longer reads a
  separate `params.json` file. Parameters are now plain Python constants
  directly in the script, between the `# ── RUN PARAMETERS ──` /
  `# ── END RUN PARAMETERS ──` comment markers — the one place Kaggle's
  own editor always shows you and lets you edit before a re-run.
- `api/index.py`'s `_push_kaggle_kernel()` was rewritten to match: instead
  of writing `params.json`, it now string-replaces that same RUN
  PARAMETERS block with the real values for the run being pushed
  (`_render_run_params_block()`), then pushes the result as a new
  version of the same kernel (`<username>/pyquant-backtest-runner`).
- A new endpoint, `POST /api/backtests/sync-to-kaggle`, calls this —
  separate from `POST /api/backtests` (the GitHub Actions path). It
  creates a `queued` run row and pushes, but does **not** start the run
  itself (unlike the GitHub Actions path) — the Backtest page's new
  "Sync to Kaggle" button shows a direct link to the kernel afterward so
  you can open it and click **Save & Run All**.

### K2 — Using it

1. Backtest page → fill in the same fields as "Run Backtest" (days,
   trials, leverage, Bayesian/ATR toggles) → click **Sync to Kaggle**
   instead.
2. The status line shows "Run #N pushed" with a link — click it to open
   the kernel on Kaggle.
3. In Kaggle's own editor, click **Save & Run All** at top right. You'll
   see Kaggle's live console output directly — no polling, no guessing.
   (Kaggle may also auto-run it without this step if the owner has
   turned on "Always run immediately on push" in the kernel's Settings;
   either way, the result lands in the same `pyquant_runs` row and the
   PyQuant UI's polling/Backtest table pick it up exactly like any other
   run.)
4. Requires `KAGGLE_USERNAME`/`KAGGLE_KEY`/`KAGGLE_DATASET_SLUG` set on
   Vercel (Part C) — same credentials Part D's fallback kernel-push path
   already needed, nothing new to configure.
5. You can also just open the kernel in Kaggle directly and hand-edit
   the RUN PARAMETERS block yourself, with no PyQuant UI round-trip at
   all — that's the whole point of moving these out of `params.json`.
   Hand edits survive until the next push from either button.

### K3 — Run-detail modal (Backtest page)

Unrelated to Kaggle specifically, but shipped the same session: clicking
any row in either Backtest-page table (Walkforward Runs, Daily Optimizer
Runs) now opens a modal with that run's full `metrics`/`params` payload,
plus its error message if it failed and its Kaggle reference if it has
one — replacing the old always-visible raw-JSON card at the bottom of
the page.
