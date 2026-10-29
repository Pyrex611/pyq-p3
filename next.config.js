/**
 * next.config.js — Next.js frontend for PyQuant Control Center.
 *
 * 2026-10-29 Next.js migration: this replaces the previous `web/` static
 * HTML/CSS/JS frontend (kept at `legacy_web/` for reference, no longer
 * deployed — see CLAUDE.md Section 3/6 for the full migration note).
 * `api/index.py`'s FastAPI app is unchanged in behavior — only how it's
 * reached changed: it used to catch EVERY request (via pyproject.toml's
 * `[tool.vercel].entrypoint`, under the old "Other" framework preset, so
 * it could also serve the static frontend itself via a StaticFiles
 * mount). Now Next.js is the detected framework and owns routing for
 * everything except `/api/*`, which `vercel.json`'s rewrite sends to the
 * Python serverless function at `api/index.py` — see that file's own
 * header comment and `vercel.json` for the other half of this.
 *
 * No rewrite is declared here: `vercel.json`'s rewrite is a platform-level
 * rule that runs before Next.js is even invoked for a `/api/*` path in
 * production, so Next.js itself never needs to know about `/api/*` at
 * all. Locally (`next dev`), there is no Python server behind `/api` by
 * default — run `uvicorn api.index:app --reload --port 8000` separately
 * and set `NEXT_PUBLIC_API_BASE`/`window.PYQUANT_API_BASE` to point at
 * it, exactly as the old static frontend's own local-dev story worked.
 */
/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
};

module.exports = nextConfig;
