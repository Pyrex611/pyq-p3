# Vercel Deployment Guide — PyQuant Control Center (API + Frontend)

This is the complete, step-by-step procedure for deploying `api/index.py`
+ `web/` to Vercel as the project `pyq-p3` (`https://pyq-p3.vercel.app`).
Follow it top to bottom on a fresh checkout — every step is included, not
just the ones that differ from "obvious" defaults.

**Why the earlier deploy 404'd on every path** (both `/` and `/api/health`):
the repo's `vercel.json` had a `"rewrites"` block pointing `/api/*` at
`/api/index.py` — but Vercel's legacy file-based model routes a function
at `api/index.py` to the path `/api` (no `.py`, no `/index` suffix), so
that rewrite's destination never matched anything real. On top of that,
`requirements.txt` at the repo root listed `fastapi`, which risks Vercel
auto-detecting the **Python framework preset** (a different routing model
where one app handles every request) while `vercel.json` was still written
for the **old file-based model** — two incompatible routing assumptions
active at once. The fix below commits fully to the framework-preset model:
`pyproject.toml`'s `[tool.vercel].entrypoint` tells Vercel exactly which
app to load, `api/index.py` itself now serves the static frontend via a
`StaticFiles` mount (so one app truly can answer everything), and
`vercel.json` is now just `{}` — no rewrites, no per-file function config,
nothing left to contradict the framework preset.

---

## Part A — One-time local tool setup

### A1. Confirm Node.js is installed (needed for the Vercel CLI)

Open PowerShell or Command Prompt on your Windows machine and run:

```
node --version
```

- If you see a version number (e.g. `v20.11.0`), skip to A2.
- If you see `'node' is not recognized...`, install Node.js:
  1. Go to https://nodejs.org in your browser.
  2. Download the **LTS** installer for Windows (the left-hand button).
  3. Run the downloaded `.msi` file, accept all defaults, click through
     the installer (Next → Next → Install → Finish).
  4. Close and reopen your terminal (PowerShell/Command Prompt) so it
     picks up the new install.
  5. Run `node --version` again to confirm it now prints a version.

### A2. Install the Vercel CLI

```
npm install -g vercel
```

Confirm it installed:

```
vercel --version
```

You should see a version number (e.g. `35.x.x`). If you instead see
`'vercel' is not recognized`, close and reopen your terminal and try again
— npm's global install path is usually only picked up by new terminal
sessions.

### A3. Log in to Vercel from the CLI

```
vercel login
```

This prints a prompt asking for your email, then sends a confirmation
link to that email address (or opens a browser for a one-click login,
depending on CLI version). Open the email/browser link, confirm, then
return to the terminal — it will print `> Success! ...` once logged in.

---

## Part B — Link this repo to the existing `pyq-p3` Vercel project

Since `pyq-p3.vercel.app` already exists (it's the project that 404'd),
you're linking your local folder to that **existing** project, not
creating a new one.

### B1. Open a terminal in the repo folder

```
cd C:\Users\Pyrex\pyq-p3
```

(Adjust the path if your checkout lives somewhere else — it must be the
folder that directly contains `vercel.json`, `pyproject.toml`, `api/`,
and `web/`.)

### B2. Run the link command

```
vercel link
```

It will ask a series of questions — answer exactly as follows:

1. `? Set up and deploy "C:\Users\Pyrex\pyq-p3"?` → type `Y`, press Enter.
2. `? Which scope should contain your project?` → select your account
   (use arrow keys, press Enter).
3. `? Link to existing project?` → type `Y`, press Enter.
4. `? What's the name of your existing project?` → type `pyq-p3`, press
   Enter.

It will confirm with something like `✅ Linked to <you>/pyq-p3`. This
creates a local `.vercel/` folder (already in `.gitignore`) that remembers
this link for future `vercel` commands.

---

## Part C — Set environment variables

Your FastAPI backend reads these at runtime (see `api/index.py`'s
`/api/config` endpoint for the full list it checks). The **only one that
actually matters for the API to function correctly** is `DATABASE_URL` —
without it, the API falls back to a local SQLite file that resets on
every deploy (Vercel's filesystem is ephemeral), so the UI would never
show persisted signals/runs. The rest (`ALPACA_*`, `BYBIT_*`,
`BINANCE_*`, `TELEGRAM_*`) are optional — they only drive the masked
"is this configured?" checkmarks on the Settings screen and are never
used by the API to call any exchange.

Set at minimum:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Your Supabase **Transaction pooler** string, port `6543` — **not** the Direct connection string, which is IPv6-only and breaks on hosts without IPv6 egress (GitHub Actions' runners among them). See `SUPABASE_SETUP.md`'s IPv6 incident note. |
| `FRONTEND_ORIGIN` | `https://pyq-p3.vercel.app` (tightens CORS; `*` also works if you'd rather skip this) |
| `PYQUANT_MODE` | `live` or `paper` — purely informational on this API; matches whatever your VPS orchestra process is actually running under |

### C1. Option 1 — CLI (fastest)

Run each of these from the repo root (replace the `<...>` placeholders).
Each command will prompt `? What's the value of DATABASE_URL?` — paste the
value and press Enter, then it asks which environments (`Production`,
`Preview`, `Development`) — press `a` to select all, then Enter.

```
vercel env add DATABASE_URL
vercel env add FRONTEND_ORIGIN
vercel env add PYQUANT_MODE
```

### ⚠️ 2026-10-25 incident: a Vercel storage integration can silently
### override a manually-set env var of the same name

If `DATABASE_URL` "isn't taking effect" — `GET /api/health` shows a
healthy Postgres connection, but it's never the same data GitHub
Actions is writing — **check for an attached storage integration
before anything else.** This project had a Neon Postgres integration
attached early on (from before Supabase was chosen) and never removed;
Vercel's own integration silently injected its own `DATABASE_URL`,
which took precedence over the manually-set Supabase value with the
same name, even after re-pasting and redeploying. Two days of DB-
identity debugging (see `PLAN.md`'s 2026-10-24/25 entries) turned out
to be this, not a GitHub Secret / Vercel env var mismatch at all.

**To check:** Vercel dashboard → the project → **Storage** tab. Any
integration listed there (Neon, Vercel Postgres, KV, Blob, etc.) that
you didn't deliberately choose to use is a candidate for this —
disconnect/remove it, then redeploy. `GET /api/health`'s `db` object
(this project's `db_ping()`) reports `current_database()`/
`inet_server_addr()` — compare that against Supabase's own dashboard to
confirm which database Vercel is actually reaching before and after.

### C2. Option 2 — Dashboard (click-through alternative, no CLI)

1. Open https://vercel.com/dashboard in your browser and log in.
2. Click the **pyq-p3** project card.
3. Click the **Settings** tab (top nav bar of the project page).
4. Click **Environment Variables** in the left-hand sidebar.
5. For each variable in the table above:
   a. In the **Key** field, type the variable name exactly (e.g.
      `DATABASE_URL`).
   b. In the **Value** field, paste the value.
   c. Under **Environments**, tick all three boxes: Production, Preview,
      Development.
   d. Click **Save**.
6. Repeat for all three variables.

---

## Part D — Deploy

### D1. Deploy to production

From the repo root:

```
vercel --prod
```

This uploads the current local files (not a git push — your earlier
GitHub push block doesn't affect this at all) and builds/deploys them.
Wait for it to print a line like:

```
✅  Production: https://pyq-p3.vercel.app [copied to clipboard]
```

If the build fails, the CLI prints the error inline — re-read it
carefully; the most common cause after this fix would be a typo in
`pyproject.toml`. You can also inspect the build in the dashboard: go to
the project page → **Deployments** tab → click the newest (top) row →
**Build Logs**.

### D2. Confirm the Framework Preset Vercel detected

1. In the dashboard, go to **pyq-p3** → **Settings** → **General**.
2. Scroll to **Build & Development Settings**.
3. Confirm **Framework Preset** shows a Python-based preset (not
   "Other" with no framework detected). If it shows "Other", click
   **Edit**, and if a Python option isn't offered in the dropdown, this
   usually self-corrects once `pyproject.toml` exists at the root for the
   *next* deploy — re-run `vercel --prod --force` (the `--force` flag
   skips Vercel's build cache, guaranteeing it re-reads `pyproject.toml`
   from scratch).

---

## Part E — Verify

Run each of these from PowerShell/Command Prompt:

```
curl https://pyq-p3.vercel.app/api/health
```
Expected: `{"ok":true}`

```
curl https://pyq-p3.vercel.app/api/status
```
Expected: `{"trading_enabled":true,"mode":"live"}` (or `"paper"`, matching
whatever you set `PYQUANT_MODE` to).

Then open `https://pyq-p3.vercel.app/` in a browser — you should see the
Dashboard page render (not a 404 JSON blob). Click through to
`/signals.html`, `/backtest.html`, `/settings.html`, `/live-monitor.html`
from the page's own nav links and confirm each one loads.

If `/api/dashboard` shows all-empty data (`"open_positions":[]`,
`"last_run":null`), that's expected until the VPS orchestra process
(`VPS_SETUP.md`) has actually run at least once and written to the
`DATABASE_URL` you configured above — the API is working correctly, it
just has nothing logged yet.

---

## Troubleshooting

- **Still 404 on every path after `vercel --prod`:** run
  `vercel --prod --force` to bypass the build cache entirely, then repeat
  Part E. Also double check `vercel.json`'s content is exactly `{}` — if
  any leftover `"rewrites"` or `"functions"` key is present, delete it.
- **404 only on `/api/...` paths, `/` works fine:** confirms the static
  mount works but the API routes aren't registering — check
  **Deployments → (latest) → Build Logs** for a Python import error (most
  likely a missing dependency not listed in `pyproject.toml`'s
  `dependencies` array).
- **500 error on any `/api/...` route:** check
  **Deployments → (latest) → Functions** (or **Runtime Logs**) tab in the
  dashboard — this shows the actual Python traceback. The most common
  cause is `DATABASE_URL` being set but malformed (see
  `SUPABASE_SETUP.md`'s "Build your `DATABASE_URL`" section for the exact
  format) or `psycopg2` failing to connect because the Supabase project
  is paused (free-tier Supabase projects pause after a week of
  inactivity — open the Supabase dashboard once to un-pause it).
- **Environment variable changes don't seem to take effect:** env var
  changes in the dashboard do **not** apply to already-built deployments
  — you must trigger a new deploy afterward (`vercel --prod` again, or in
  the dashboard: **Deployments** → latest row → **⋯** menu → **Redeploy**).
