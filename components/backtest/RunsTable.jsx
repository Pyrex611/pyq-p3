import React from "react";
import { fmtTime, pnlClass } from "../../lib/api";

export default function RunsTable({ wfRuns, optRuns, onSelectRun, onRefresh, isAnyRunActive }) {
  return (
    <>
      {isAnyRunActive && (
        <div className="banner info" style={{ marginBottom: 16 }}>
          <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: "var(--amber)", marginRight: 6 }}></span>
          A simulation run is currently in progress. The runs table will auto-update without requiring manual refresh.
        </div>
      )}

      <div className="grid cols-2">
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <h3 style={{ margin: 0 }}>Walkforward Backtest Runs</h3>
            <button
              type="button"
              className="btn"
              style={{ padding: "3px 8px", fontSize: 11 }}
              onClick={onRefresh}
            >
              Refresh
            </button>
          </div>
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>Total Return</th>
                <th>Win Rate</th>
                <th>Trades</th>
                <th>Finished</th>
              </tr>
            </thead>
            <tbody>
              {wfRuns &&
                wfRuns.map((r) => {
                  const m = r.metrics || {};
                  const retVal = m["Total Return (%)"];
                  return (
                    <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => onSelectRun(r.id)}>
                      <td className="mono" style={{ fontWeight: 600 }}>#{r.id}</td>
                      <td className={`mono ${pnlClass(retVal)}`}>{retVal !== undefined ? `${retVal}%` : "—"}</td>
                      <td className="mono">{m["Win Rate (%)"] !== undefined ? `${m["Win Rate (%)"]}%` : "—"}</td>
                      <td className="mono">{m["Total Trades"] ?? m.trade_count ?? "—"}</td>
                      <td>{fmtTime(r.finished_at)}</td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
          {wfRuns && wfRuns.length === 0 && (
            <div className="empty-state">No walkforward runs logged yet. Execute one above to populate telemetry.</div>
          )}
        </div>

        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <h3 style={{ margin: 0 }}>Daily Optimizer Runs</h3>
            <span style={{ fontSize: 11, color: "var(--text-faint)" }}>UTC 00:03 Daily</span>
          </div>
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>Status</th>
                <th>Finished</th>
              </tr>
            </thead>
            <tbody>
              {optRuns &&
                optRuns.map((r) => (
                  <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => onSelectRun(r.id)}>
                    <td className="mono" style={{ fontWeight: 600 }}>#{r.id}</td>
                    <td>
                      <span className={`pill ${r.status === "completed" ? "closed_tp" : "closed_sl"}`}>
                        {r.status?.toUpperCase() || "DONE"}
                      </span>
                    </td>
                    <td>{fmtTime(r.finished_at)}</td>
                  </tr>
                ))}
            </tbody>
          </table>
          {optRuns && optRuns.length === 0 && (
            <div className="empty-state">No optimizer runs recorded yet.</div>
          )}
        </div>
      </div>
    </>
  );
}