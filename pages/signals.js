import { useEffect, useState } from "react";
import Layout from "../components/Layout";
import { apiGet, fmtPct, fmtPrice, fmtTime, pnlClass, statusLabel } from "../lib/api";

// 2026-11-13 (owner UI request): "the rows in the signals page should be
// clickable to reveal all the details on that signal, the position, and
// every data regarding that tick as is currently being collected."
// Dumps every non-null field from the signal row and the nearest live/
// backtest diagnostic rows (GET /api/signals/{id}/detail, which reuses
// Compare's own _nearest_gate_states() lookup) as plain key/value pairs —
// deliberately generic rather than hand-picking fields, so a new
// diagnostic column (there have been several this project: ukf_velocity,
// raw_close, fresh_pred, signal_source_pred, train window, …) shows up
// here automatically without this modal needing its own update.
function KVTable({ obj }) {
  if (!obj) return <div className="empty-state" style={{ padding: 8 }}>No data recorded for this tick.</div>;
  const entries = Object.entries(obj).filter(([, v]) => v !== null && v !== undefined && v !== "");
  if (entries.length === 0) return <div className="empty-state" style={{ padding: 8 }}>No data recorded for this tick.</div>;
  return (
    <table style={{ fontSize: 11 }}>
      <tbody>
        {entries.map(([k, v]) => (
          <tr key={k}>
            <td className="mono" style={{ color: "var(--text-faint)", whiteSpace: "nowrap" }}>{k}</td>
            <td className="mono" style={{ wordBreak: "break-all" }}>{typeof v === "object" ? JSON.stringify(v) : String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SignalDetailModal({ signalId, onClose }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    apiGet(`/signals/${signalId}/detail`)
      .then((d) => { if (!cancelled) setDetail(d); })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [signalId]);

  return (
    <div
      className="modal-overlay"
      style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 }}
      onClick={onClose}
    >
      <div
        className="modal-card"
        style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: 10, padding: 20, maxWidth: 820, width: "92%", maxHeight: "85vh", overflowY: "auto" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Signal #{signalId} — Full Detail</h3>
          <button type="button" className="btn" onClick={onClose}>Close</button>
        </div>
        {error && <div className="banner error">{error}</div>}
        {!detail && !error && <div className="hint">Loading…</div>}
        {detail && (
          <div className="grid cols-2" style={{ gap: 16 }}>
            <div>
              <h4 style={{ margin: "0 0 6px 0", fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase" }}>
                Signal / Position
              </h4>
              <KVTable obj={detail.signal} />
            </div>
            <div>
              <h4 style={{ margin: "0 0 6px 0", fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase" }}>
                Live Diagnostics (nearest tick)
              </h4>
              <KVTable obj={detail.live_diagnostics} />
            </div>
            {detail.backtest_diagnostics && (
              <div style={{ gridColumn: "1 / -1" }}>
                <h4 style={{ margin: "0 0 6px 0", fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase" }}>
                  Backtest Diagnostics (Run #{detail.backtest_run_id}, same hour, for reference)
                </h4>
                <KVTable obj={detail.backtest_diagnostics} />
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default function Signals() {
  const [symbol, setSymbol] = useState("");
  const [status, setStatus] = useState("");
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [selectedId, setSelectedId] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/signals", { symbol, status })
      .then((d) => { if (!cancelled) { setData(d); setError(null); } })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [symbol, status]);

  return (
    <Layout title="Signals" footer="Up to 200 most recent signals logged by the trading engine (pyquant_signals).">
      {error && <div className="banner error">Could not reach the API ({error}).</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Filter</h3>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <select
            className="mono"
            style={{ background: "var(--bg)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 6, padding: "6px 10px" }}
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
          >
            <option value="">All symbols</option>
            <option value="BTCUSDT">BTCUSDT</option>
            <option value="ETHUSDT">ETHUSDT</option>
          </select>
          <select
            className="mono"
            style={{ background: "var(--bg)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 6, padding: "6px 10px" }}
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            <option value="">All statuses</option>
            <option value="open">Open</option>
            <option value="closed_tp">Closed — TP</option>
            <option value="closed_sl">Closed — SL</option>
            <option value="closed_manual">Closed — Manual</option>
          </select>
        </div>
      </div>

      <div className="card">
        <h3>Signal History</h3>
        <table>
          <thead>
            <tr>
              <th>Symbol</th><th>Side</th><th>Status</th><th>Entry</th><th>Outcome</th>
              <th>PnL</th><th>Mode</th><th>Opened</th><th>Closed</th>
            </tr>
          </thead>
          <tbody>
            {data && data.signals.map((s, i) => (
              <tr key={i} style={{ cursor: "pointer" }} onClick={() => setSelectedId(s.id)} title="Click for full detail">
                <td className="mono">{s.symbol}</td>
                <td className={s.side === "BUY" ? "side-buy" : "side-sell"}>{s.side}</td>
                <td>
                  <span className={`pill ${s.status}`}>{statusLabel(s.status)}</span>
                  {s.is_ghosted && (
                    <span
                      className="pill"
                      style={{ marginLeft: 6, background: "var(--accent-soft)", color: "var(--accent-soft-text)" }}
                      title="Opened while a previous position on this symbol was still open — logged, but excluded from Avg. Return since the live engine's equity was still committed to the earlier trade."
                    >
                      GHOSTED
                    </span>
                  )}
                </td>
                <td className="mono">{fmtPrice(s.entry_price)}</td>
                <td className="mono">{fmtPrice(s.outcome_price)}</td>
                <td className={pnlClass(s.realized_pnl_pct ?? 0)}>
                  {fmtPct(s.realized_pnl_pct ?? 0)}
                </td>
                <td>{s.mode}</td>
                <td>{fmtTime(s.opened_at)}</td>
                <td>{fmtTime(s.closed_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.signals.length === 0 && <div className="empty-state">No signals match this filter.</div>}
      </div>
      {selectedId !== null && (
        <SignalDetailModal signalId={selectedId} onClose={() => setSelectedId(null)} />
      )}
    </Layout>
  );
}
