/**
 * lib/api.js — ported verbatim (behavior unchanged) from the former
 * web/assets/style.css's sibling web/assets/app.js, as ES module exports
 * for the Next.js frontend (2026-10-29 migration — see CLAUDE.md/PLAN.md).
 *
 * API base resolution, in order:
 *   1. window.PYQUANT_API_BASE set at runtime (useful for a two-project
 *      deploy: Next.js frontend on one Vercel project, API on another).
 *   2. process.env.NEXT_PUBLIC_API_BASE, for local dev against a
 *      separately-run `uvicorn api.index:app` (see next.config.js's
 *      header comment).
 *   3. "/api" — the default, correct in production where vercel.json's
 *      rewrite routes /api/* to the api/index.py serverless function
 *      sharing this same Vercel project.
 */
function apiBase() {
  if (typeof window !== "undefined" && window.PYQUANT_API_BASE) {
    return window.PYQUANT_API_BASE;
  }
  return process.env.NEXT_PUBLIC_API_BASE || "/api";
}

/* Found during the 2026-10-20 "502 with no further detail" investigation:
 * both of these used to throw away the response body on a non-2xx status,
 * so the UI only ever showed a bare status code (e.g. "POST /backtests
 * failed: 502") even though the API's HTTPException almost always carries
 * a real, useful `detail` string explaining exactly what went wrong. This
 * helper reads that body (JSON `detail`/`error`, falling back to raw
 * text) and folds it into the thrown Error's message so every caller gets
 * it for free. */
async function _errorDetail(res) {
  try {
    const text = await res.text();
    try {
      const j = JSON.parse(text);
      return j.detail || j.error || text;
    } catch {
      return text;
    }
  } catch {
    return null;
  }
}

export async function apiGet(path, params) {
  const base = apiBase();
  const origin = typeof window !== "undefined" ? window.location.origin : "http://localhost";
  const url = new URL(base + path, origin);
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
    });
  }
  const res = await fetch(url.toString());
  if (!res.ok) {
    const detail = await _errorDetail(res);
    throw new Error(`GET ${path} failed: ${res.status}${detail ? ` — ${detail}` : ""}`);
  }
  return res.json();
}

export async function apiPost(path, body) {
  const res = await fetch(apiBase() + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) {
    const detail = await _errorDetail(res);
    throw new Error(`POST ${path} failed: ${res.status}${detail ? ` — ${detail}` : ""}`);
  }
  return res.json();
}

export function fmtMoney(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  const sign = n > 0 ? "+" : "";
  return `${sign}$${n.toFixed(2)}`;
}

export function fmtPct(v) {
  // 2026-10-25 (PnL-by-percentage): v is a fraction (0.0183 = "+1.83%"),
  // already leveraged — see pyquant_signal_math.outcome_pct().
  if (v === null || v === undefined) return "—";
  const n = Number(v) * 100;
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

export function fmtPrice(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export function fmtTime(v) {
  if (!v) return "—";
  try {
    const d = new Date(v);
    return d.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return v;
  }
}

export function pnlClass(v) {
  if (v === null || v === undefined) return "";
  return Number(v) >= 0 ? "pnl-pos" : "pnl-neg";
}

export function statusLabel(status) {
  const map = {
    open: "OPEN", closed_tp: "TP HIT", closed_sl: "SL HIT", closed_manual: "CLOSED",
  };
  return map[status] || (status || "").toString().toUpperCase();
}

/** Re-derives a RAW (unleveraged) price-return fraction from a signal's
 *  own entry_price/outcome_price/side — the exact inverse of
 *  pyquant_signal_math.outcome_pct()'s leveraged formula — so the
 *  Dashboard's leverage override can re-apply any leverage to it,
 *  entirely client-side, with no backend round trip and no change to
 *  what's actually stored. Ported from web/index.html's inline script
 *  (2026-10-28). */
export function rawPct(side, entry, exit) {
  if (entry === null || entry === undefined || exit === null || exit === undefined || !entry) return null;
  const isBuy = (side || "").toString().toUpperCase().startsWith("B");
  return isBuy ? (exit - entry) / entry : (entry - exit) / entry;
}
