import { useEffect, useState } from "react";
import Layout from "../components/Layout";
import { apiGet, fmtPct, fmtPrice, fmtTime, pnlClass, statusLabel } from "../lib/api";

export default function Signals() {
  const [symbol, setSymbol] = useState("");
  const [status, setStatus] = useState("");
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

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
              <tr key={i}>
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
    </Layout>
  );
}
