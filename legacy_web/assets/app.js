/* PyQuant Control Center — shared frontend logic.
 *
 * This is a genuinely separate, deployable static frontend (plain HTML/CSS/
 * JS) — NOT an edit to the Design-canvas .dc.html mockup. It fetches real
 * data from the Phase 2 FastAPI backend (api/index.py) over HTTP.
 *
 * API base resolution, in order:
 *   1. window.PYQUANT_API_BASE set inline on the page (useful for a
 *      two-project deploy: static site on one Vercel project, API on
 *      another).
 *   2. "/api" — the default, correct when the frontend and API share one
 *      Vercel project (vercel.json's rewrite routes /api/* to api/index.py
 *      already; this is the zero-config path).
 */
const API_BASE = window.PYQUANT_API_BASE || "/api";

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

async function apiGet(path, params) {
  const url = new URL(API_BASE + path, window.location.origin);
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

async function apiPost(path, body) {
  const res = await fetch(API_BASE + path, {
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

function fmtMoney(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  const sign = n > 0 ? "+" : "";
  return `${sign}$${n.toFixed(2)}`;
}

function fmtPct(v) {
  // 2026-10-25 (PnL-by-percentage): v is a fraction (0.0183 = "+1.83%"),
  // already leveraged — see pyquant_signal_math.outcome_pct().
  if (v === null || v === undefined) return "—";
  const n = Number(v) * 100;
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

function fmtPrice(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function fmtTime(v) {
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

function pnlClass(v) {
  if (v === null || v === undefined) return "";
  return Number(v) >= 0 ? "pnl-pos" : "pnl-neg";
}

function statusLabel(status) {
  const map = {
    open: "OPEN", closed_tp: "TP HIT", closed_sl: "SL HIT", closed_manual: "CLOSED",
  };
  return map[status] || status.toUpperCase();
}

/** Renders the LIVE/PAPER/OFF switch in the topbar and wires the OFF toggle
 *  to POST /api/status. LIVE/PAPER are shown as the orchestra's current
 *  configured mode (informational — see api/index.py's /api/status
 *  docstring for why switching exchange credentials isn't done from here);
 *  OFF is the one real, live control. */
async function mountModeSwitch(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  try {
    const status = await apiGet("/status");
    renderModeSwitch(el, status);
  } catch (e) {
    el.innerHTML = `<span style="color:var(--text-faint); font-size:12px;">status unavailable</span>`;
  }
}

function renderModeSwitch(el, status) {
  const mode = status.mode === "paper" ? "PAPER" : "LIVE";
  const enabled = status.trading_enabled;
  el.innerHTML = `
    <button type="button" class="${mode === 'LIVE' ? 'active' : ''}" disabled>LIVE</button>
    <button type="button" class="${mode === 'PAPER' ? 'active' : ''}" disabled>PAPER</button>
    <button type="button" id="off-toggle" class="${!enabled ? 'off active' : ''}">
      ${enabled ? 'TURN OFF' : 'OFF (tap to resume)'}
    </button>
  `;
  const toggle = el.querySelector("#off-toggle");
  toggle.addEventListener("click", async () => {
    toggle.disabled = true;
    try {
      const next = await apiPost("/status", { enabled: !enabled });
      renderModeSwitch(el, next);
    } catch (e) {
      toggle.disabled = false;
      alert("Could not update trading switch: " + e.message);
    }
  });
}

function highlightActiveNav() {
  const here = window.location.pathname.split("/").pop() || "index.html";
  document.querySelectorAll("nav.primary a").forEach((a) => {
    const href = a.getAttribute("href");
    if (href === here) a.classList.add("active");
  });
}

document.addEventListener("DOMContentLoaded", () => {
  highlightActiveNav();
  mountModeSwitch("mode-switch");
});
