import React from "react";
import { pyqInsight } from "../lib/api";

/**
 * Shared gate-state readout — one model's volatility/strict-entry gate
 * state (plus a PyQ Insights plain-language line) for a single tick.
 * 2026-11-03: extracted out of RunDetailModal.jsx so the same rendering
 * can be reused by the Compare page's hour-level model-state comparison
 * (pages/compare.js) without duplicating this markup. Works on either a
 * live pyquant_diagnostics_history row or a backtest
 * pyquant_backtest_diagnostics row — both share the same field names
 * (see pyquant_state.py's _DIAG_HISTORY_COLS / _BACKTEST_DIAG_COLS).
 */
export default function GateReadout({ diag, emptyLabel }) {
  if (!diag) {
    return (
      <div style={{ color: "var(--text-muted)" }}>
        {emptyLabel || "No diagnostic record within the comparison window."}
      </div>
    );
  }
  return (
    <>
      <div>Trend Decision: <span className="mono">{diag.trend?.toUpperCase() || "—"}</span></div>
      <div>
        Vol Gate:{" "}
        <span style={{ color: diag.vol_gate_passed ? "var(--green)" : "var(--amber)" }}>
          {diag.vol_gate_passed ? "PASSED" : "BLOCKED"}
        </span>{" "}
        ({((diag.pct_diff || 0) * 100).toFixed(3)}% vs{" "}
        {((diag.vol_gate_threshold || 0) * 100).toFixed(3)}%)
      </div>
      <div>
        Strict Entry Gate:{" "}
        <span style={{ color: diag.strict_gate_passed ? "var(--green)" : "var(--amber)" }}>
          {diag.strict_gate_passed ? "PASSED" : "BLOCKED"}
        </span>{" "}
        ({((diag.strict_diff || 0) * 100).toFixed(3)}% vs{" "}
        {((diag.strict_threshold || 0) * 100).toFixed(3)}%)
      </div>
      <div>
        Signal Fired:{" "}
        <span style={{ fontWeight: 600, color: diag.signal_fired ? "var(--green)" : "var(--text-faint)" }}>
          {diag.signal_fired ? "YES" : "NO"}
        </span>
      </div>
      <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px dashed var(--border)", color: "var(--text-muted)", fontSize: 11, lineHeight: 1.5 }}>
        {pyqInsight(diag)}
      </div>
    </>
  );
}
