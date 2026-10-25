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

**What this integration is:** the UI's "Run Backtest" button calls
`POST /api/backtests` on the Vercel API, which writes an immediate
`queued` row (so the UI has something to show right away) and triggers a
Kaggle kernel run via Kaggle's API — no polling loop needed, since the
Kaggle API call itself is the trigger. The kernel (`kaggle_backtest/
run_backtest.py`) clones this public repo, runs `walkforward.py` with
your chosen parameter overrides, and writes its result straight back to
the same shared Supabase Postgres database the rest of the platform
already uses — the UI just keeps polling `GET /api/runs/{id}`, exactly
like any other run.

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
| `KAGGLE_DATASET_SLUG` | `yourkaggleusername/pyquant-historical-data` — the **bare** `owner/slug` form only, NOT the full dataset page URL (`https://www.kaggle.com/datasets/owner/slug`). Pasting the URL by mistake is exactly the error a live run hit on 2026-10-24: Kaggle's API rejected it with "Invalid dataset specification." `api/index.py`'s `create_backtest()` now defensively strips a pasted URL down to the bare slug, but set it correctly here anyway. (create this dataset in Part E below first, or come back and set this after) |

3. Redeploy the Vercel project (**Deployments** tab → the three-dot menu
   on the latest deployment → **Redeploy**) so the new env vars take
   effect — Vercel doesn't pick up new environment variables on already-
   running deployments.

## Part D — Create the Kaggle kernel

### ⚠️ 2026-10-25 incident: kernel type mismatch → 409 Conflict

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

## Part G — Test it

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

**Leaning recommendation if/when this gets built:** option 1, run from
`daily_optimizer()` rather than the API — it reuses data already being
downloaded for another reason, needs no new credential surface beyond
one more Kaggle secret in the same place `SIGNAL_ONLY_MODE`/
`PYQUANT_MODE` already live, and keeps the "Vercel never does heavy
work" rule intact. Not yet designed in code-level detail or built —
tracked as a backlog candidate in `PLAN.md` if the owner wants to pick
it up.
