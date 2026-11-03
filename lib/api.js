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

// Human-readable version of SignalGenerator.last_diagnostic's
// stage_reached (p4_utils.py) — added 2026-10-31 after feedback that
// the raw diagnostic fields were "hard to understand at first glance".
// Shared between the Live Monitor page's latest-tick table and its new
// diagnostics-history section so both read the same way.
export function diagStageLabel(dg) {
  if (!dg) return "—";
  switch (dg.stage_reached) {
    case "signal_fired": return "Signal fired";
    case "strict_gate": return dg.strict_gate_passed ? "Entry gate cleared" : "Blocked at entry gate";
    case "vol_gate": return dg.vol_gate_passed ? "Vol gate cleared" : "Blocked at volatility gate";
    case "flat": return "No directional move yet";
    case "flat_pred_eq_price": return "Flat — prediction equals price";
    case "unknown_symbol": return "Unknown symbol (config issue)";
    case "no_ukf_data": return "No UKF data this tick";
    case "exception": return `Error: ${dg.error || "unknown"}`;
    default: return dg.stage_reached || "—";
  }
}

// 2026-11-03 ("PyQ Insights"): diagStageLabel() above gives a short
// label ("Blocked at entry gate"); this gives the full plain-language
// explanation the owner asked for after finding the Signal Diagnostics
// History chart "really isn't easily understood... just a chart, no
// explanations" — translating the same raw gate numbers (pct_diff/vol
// vs. their thresholds, strict_diff vs. its threshold) into a sentence
// a non-engineer reading the dashboard can act on: which threshold(s)
// crossed, which didn't, and what the strategy is waiting for. Shared
// by the Live Monitor history chart, the backtest trade drill-down's
// "Compare to Live" drawer (both the live AND backtest sides, now that
// the backtest persists the same diagnostic shape — see
// pyquant_state.record_backtest_diagnostics()), and the Compare page's
// hour-level gate comparison. Works on either a live diagnostics_history
// row or a backtest pyquant_backtest_diagnostics row — same field names.
export function pyqInsight(dg) {
  if (!dg) return "No diagnostic data for this point.";
  if (dg.error) return `Error evaluating this tick: ${dg.error}`;
  if (dg.stage_reached === "unknown_symbol") return "Unknown symbol — a configuration issue, not a market read.";
  if (dg.stage_reached === "no_ukf_data") return "No UKF prediction was available for this tick yet.";
  if (!dg.trend || dg.stage_reached === "flat" || dg.stage_reached === "flat_pred_eq_price") {
    return "Price and the UKF prediction are flat relative to each other — no directional edge for either gate to act on.";
  }

  const pct = ((dg.pct_diff ?? 0) * 100).toFixed(3);
  const vol = ((dg.vol ?? 0) * 100).toFixed(3);
  const volThresh = ((dg.vol_gate_threshold ?? 0) * 100).toFixed(3);
  const strictDiff = dg.strict_diff != null ? (dg.strict_diff * 100).toFixed(3) : null;
  const strictThresh = ((dg.strict_threshold ?? 0) * 100).toFixed(3);
  const dir = dg.trend === "up" ? "an upward" : "a downward";

  if (!dg.vol_gate_passed) {
    return (
      `UKF called ${dir} move, but the volatility gate did NOT cross: price is ${pct}% from the ` +
      `prediction and the predicted band is ${vol}% wide — neither cleared the ${volThresh}% threshold ` +
      `needed. Too quiet/uncertain a move to act on yet; waiting for a bigger one.`
    );
  }

  if (!dg.strict_gate_passed) {
    // 2026-11-03: the strict gate isn't a single ">=" check on strict_diff —
    // it's "strict_diff >= strict_threshold AND the predicted band sits on
    // the correct side of pred" (buy_price < pred for an up move, sell_price
    // > pred for a down move — see walkforward.diagnose_gates()/p4_utils.py's
    // SignalGenerator.generate_signal(), which this mirrors). strict_diff is
    // still computed even when that second, structural condition fails, so
    // it's possible to see strict_diff >= strict_threshold and BLOCKED at
    // the same time. That's NOT "needs more retrace" — more movement in that
    // direction won't fix it, because the model's own predicted band is
    // positioned on the wrong side of its central prediction for this trend.
    // Found from a real example where live showed strict_diff=1.238% vs a
    // 0.410% threshold yet still BLOCKED.
    const bandInverted = strictDiff !== null && dg.strict_diff >= dg.strict_threshold;
    if (bandInverted) {
      const bandSide = dg.trend === "up" ? "below" : "above";
      return (
        `Volatility threshold CROSSED (${pct}% move vs ${volThresh}% needed), and the ${strictDiff}% gap ` +
        `is already past the ${strictThresh}% strict-entry threshold — but the gate is still BLOCKED: for ` +
        `${dir} move, the predicted band needs to sit ${bandSide} the central prediction, and right now it ` +
        `doesn't. This isn't a "wait for more retrace" situation — the band itself is inverted at this tick, ` +
        `so no amount of further movement in this direction fires it. Usually transient; re-check next tick.`
      );
    }
    return (
      `Volatility threshold CROSSED (${pct}% move vs ${volThresh}% needed) — but the strict entry gate ` +
      `has NOT crossed yet` +
      (strictDiff !== null ? ` (${strictDiff}% retraced of ${strictThresh}% needed)` : ` (needs ${strictThresh}% retrace)`) +
      `. Waiting for retrace before entry.`
    );
  }

  return (
    `Both gates crossed — volatility (${pct}% vs ${volThresh}% needed) and strict entry ` +
    `(${strictDiff ?? "?"}% vs ${strictThresh}% needed)` +
    `${dg.signal_fired ? " — a signal fired." : "."}`
  );
}

// Compact badge form of pyqInsight() — {label, tone} for small chips
// (tone maps to the pill classes already in styles/globals.css:
// "closed_tp" green, "closed_sl" red/amber-ish via "open", etc. — callers
// pick their own className, this just classifies the state).
export function pyqInsightTone(dg) {
  if (!dg || dg.error) return "unknown";
  if (!dg.trend || dg.stage_reached === "flat" || dg.stage_reached === "flat_pred_eq_price") return "flat";
  if (dg.signal_fired) return "fired";
  if (dg.vol_gate_passed && !dg.strict_gate_passed) {
    // see the matching bandInverted check in pyqInsight() above
    const bandInverted = dg.strict_diff != null && dg.strict_diff >= dg.strict_threshold;
    return bandInverted ? "blocked_band_invalid" : "waiting_retrace";
  }
  if (!dg.vol_gate_passed) return "blocked_vol";
  return "unknown";
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
