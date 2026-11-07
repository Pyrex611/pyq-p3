import React from "react";

/**
 * Full field-by-field live-vs-backtest comparison, with a variance
 * column — 2026-11-06. GateReadout only ever showed the gate PASS/FAIL
 * verdicts; this is for actually pinpointing a divergence: every raw
 * number (price/pred/high_pred/low_pred/buy_price/sell_price) and the
 * training window each model's UKF was re-anchored on, side by side, so
 * a discrepancy (or its absence) is visible directly instead of inferred
 * from gate outcomes alone. Shared by pages/compare.js's
 * ModelStateComparison and components/backtest/RunDetailModal.jsx's
 * "Compare to Live" drawer — both already use the same diagnostic field
 * names (pyquant_state.py's _DIAG_HISTORY_COLS / _BACKTEST_DIAG_COLS),
 * same as GateReadout.
 *
 * Numeric fields get an absolute AND relative (%) diff; non-numeric
 * fields get a MATCH/DIFFER verdict. A field blank on either side shows
 * "—" with no diff computed (nothing to compare).
 */
export default function FullComparisonTable({ backtest, live }) {
  if (!backtest && !live) return null;

  const NUMERIC_ROWS = [
    ["price", "Price"],
    ["pred", "Pred (central UKF prediction)"],
    ["high_pred", "High Pred (upper band)"],
    ["low_pred", "Low Pred (lower band)"],
    ["buy_price", "Buy Price (entry band, up-trend)"],
    ["sell_price", "Sell Price (entry band, down-trend)"],
    ["pct_diff", "Pct Diff (trend magnitude)"],
    ["vol", "Vol (volatility gate input)"],
    ["vol_gate_threshold", "Vol Gate Threshold"],
    ["strict_diff", "Strict Diff (entry gate input)"],
    ["strict_threshold", "Strict Gate Threshold"],
    ["train_bar_count", "Training Bar Count"],
  ];
  const TEXT_ROWS = [
    ["trend", "Trend Decision"],
    ["vol_gate_passed", "Vol Gate Passed"],
    ["strict_gate_passed", "Strict Gate Passed"],
    ["signal_fired", "Signal Fired"],
    ["stage_reached", "Stage Reached"],
    ["ts", "Row Timestamp (UTC)"],
    ["train_window_start", "Training Window Start"],
    ["train_window_end", "Training Window End"],
  ];

  function fmtVal(v, numeric) {
    if (v === null || v === undefined) return "—";
    if (typeof v === "boolean") return v ? "YES" : "NO";
    if (numeric && typeof v === "number") {
      return Math.abs(v) < 10 ? v.toFixed(6) : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
    }
    return String(v);
  }

  function diffCell(bVal, lVal, numeric) {
    if (bVal === null || bVal === undefined || lVal === null || lVal === undefined) {
      return <span style={{ color: "var(--text-faint)" }}>—</span>;
    }
    if (numeric && typeof bVal === "number" && typeof lVal === "number") {
      const absDiff = lVal - bVal;
      const pctDiff = bVal !== 0 ? (absDiff / Math.abs(bVal)) * 100 : null;
      const big = pctDiff !== null && Math.abs(pctDiff) > 5; // >5% relative divergence flagged
      return (
        <span className={big ? "delta-neg" : ""} style={{ fontWeight: big ? 700 : 400 }}>
          {absDiff >= 0 ? "+" : ""}{Math.abs(absDiff) < 10 ? absDiff.toFixed(6) : absDiff.toLocaleString(undefined, { maximumFractionDigits: 2 })}
          {pctDiff !== null ? ` (${pctDiff >= 0 ? "+" : ""}${pctDiff.toFixed(2)}%)` : ""}
        </span>
      );
    }
    const match = String(bVal) === String(lVal);
    return (
      <span className={match ? "" : "delta-neg"} style={{ fontWeight: match ? 400 : 700 }}>
        {match ? "MATCH" : "DIFFER"}
      </span>
    );
  }

  const rows = [...NUMERIC_ROWS.map(([k, label]) => [k, label, true]),
                ...TEXT_ROWS.map(([k, label]) => [k, label, false])];

  return (
    <div style={{ marginTop: 16 }}>
      <div style={{ fontSize: 11, color: "var(--text-faint)", textTransform: "uppercase", marginBottom: 6 }}>
        Full Field Comparison (raw values + training window — not just gate verdicts)
      </div>
      <div style={{ overflowX: "auto" }}>
        <table style={{ fontSize: 12, width: "100%" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>Field</th>
              <th style={{ textAlign: "left" }}>Backtest</th>
              <th style={{ textAlign: "left" }}>Live</th>
              <th style={{ textAlign: "left" }}>Diff / Variance</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([key, label, numeric]) => {
              const bVal = backtest ? backtest[key] : null;
              const lVal = live ? live[key] : null;
              return (
                <tr key={key}>
                  <td className="mono">{label}</td>
                  <td className="mono">{fmtVal(bVal, numeric)}</td>
                  <td className="mono">{fmtVal(lVal, numeric)}</td>
                  <td className="mono">{diffCell(bVal, lVal, numeric)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="hint" style={{ marginTop: 6 }}>
        Rows flagged in red (numeric: &gt;5% relative difference; text: DIFFER) are the fields most likely
        to explain a live-vs-backtest divergence at this hour. Training Window Start/End/Bar Count show
        exactly what historical data each model's UKF was re-anchored on going into this hour — if those
        windows don't line up, the rest of the row's differences are expected, not a bug.
      </div>
    </div>
  );
}
