import { useEffect, useState } from "react";
import Layout from "../components/Layout";
import { apiGet, fmtPct, fmtPrice, fmtTime, pnlClass } from "../lib/api";

function StatCard({ label, value, cls }) {
  return (
    <div>
      <div className={`stat-value ${cls || ""}`}>{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

export default function Compare() {
  const [data, setData] = useState(null);
  const [daily, setDaily] = useState(null);
  const [error, setError] = useState(null);
  const [expandedDay, setExpandedDay] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/compare")
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(e.message));
    apiGet("/compare/daily")
      .then((d) => !cancelled && setDaily(d))
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, []);

  const live = data?.live || {};
  const wf = data?.latest_walkforward_run;
  const wfMetrics = wf?.metrics || {};

  const liveByDay = daily?.live_by_day || {};
  const backtestByDay = daily?.backtest_by_day || {};
  const allDates = Array.from(new Set([...Object.keys(liveByDay), ...Object.keys(backtestByDay)])).sort().reverse();

  return (
    <Layout title="Comparative Analytics" footer="Comparison aligns Live signals against Backtest trades by calendar date.">
      {error && <div className="banner error">Connection Notice: {error}</div>}

      {/* Aggregate Header Comparison */}
      <div className="grid cols-2" style={{ marginBottom: 16 }}>
        <div className="card">
          <h3>Live Tracking Aggregate</h3>
          <div className="grid cols-2" style={{ gap: 14 }}>
            <StatCard label="Avg Return (%)" value={fmtPct(live.avg_pnl_pct)} cls={pnlClass(live.avg_pnl_pct)} />
            <StatCard label="Win Rate (%)" value={live.win_rate_pct != null ? `${live.win_rate_pct.toFixed(1)}%` : "—"} />
            <StatCard label="Total Trades" value={live.trade_count ?? "—"} />
            <StatCard label="Win / Loss Count" value={`${live.win_count ?? 0} / ${live.loss_count ?? 0}`} />
          </div>
          <div className="hint" style={{ marginTop: 10 }}>
            {data?.signals_baseline_at ? `Baseline Active: Since ${fmtTime(data.signals_baseline_at)}` : "All Time"}
          </div>
        </div>

        <div className="card">
          <h3>Latest Walkforward Backtest</h3>
          <div className="grid cols-2" style={{ gap: 14 }}>
            <StatCard label="Total Return (%)" value={wfMetrics["Total Return (%)"] ? `${wfMetrics["Total Return (%)"]}%` : "—"} cls={pnlClass(wfMetrics["Total Return (%)"])} />
            <StatCard label="Win Rate (%)" value={wfMetrics["Win Rate (%)"] ? `${wfMetrics["Win Rate (%)"]}%` : "—"} />
            <StatCard label="Total Trades" value={wfMetrics["Total Trades"] ?? wfMetrics.trade_count ?? "—"} />
            <StatCard label="Run Reference" value={wf ? `#${wf.id}` : "—"} />
          </div>
          <div className="hint" style={{ marginTop: 10 }}>
            {wf ? `Finished: ${fmtTime(wf.finished_at)}` : "No completed backtest found."}
          </div>
        </div>
      </div>

      {/* Granular Master-Detail Date Drilldown Table */}
      <div className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10 }}>
          <h3 style={{ margin: 0 }}>Day-by-Day Trade Reconciliation</h3>
          <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
            Click any row to drill down into matching trades
          </span>
        </div>

        <div className="daily-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date (UTC)</th>
                <th>Live Trades</th>
                <th>Live Win %</th>
                <th>Live Avg %</th>
                <th>Backtest Trades</th>
                <th>Backtest Win %</th>
                <th>Backtest Avg %</th>
                <th>Return Delta (Live - Sim)</th>
              </tr>
            </thead>
            <tbody>
              {allDates.map((day) => {
                const l = liveByDay[day];
                const b = backtestByDay[day];
                const delta = l && b && l.avg_pnl_pct !== null && b.avg_pnl_pct !== null ? l.avg_pnl_pct - b.avg_pnl_pct : null;
                const isExpanded = expandedDay === day;

                return (
                  <React.Fragment key={day}>
                    <tr
                      style={{ cursor: "pointer", background: isExpanded ? "var(--border-soft)" : "transparent" }}
                      onClick={() => setExpandedDay(isExpanded ? null : day)}
                    >
                      <td className="mono" style={{ fontWeight: 600 }}>
                        {isExpanded ? "▼" : "▶"} {day}
                      </td>
                      <td className="mono">{l ? l.trade_count : "—"}</td>
                      <td className="mono">{l?.win_rate_pct != null ? `${l.win_rate_pct.toFixed(1)}%` : "—"}</td>
                      <td className={`mono ${pnlClass(l?.avg_pnl_pct)}`}>{l?.avg_pnl_pct != null ? fmtPct(l.avg_pnl_pct) : "—"}</td>
                      <td className="mono">{b ? b.trade_count : "—"}</td>
                      <td className="mono">{b?.win_rate_pct != null ? `${b.win_rate_pct.toFixed(1)}%` : "—"}</td>
                      <td className={`mono ${pnlClass(b?.avg_pnl_pct)}`}>{b?.avg_pnl_pct != null ? fmtPct(b.avg_pnl_pct) : "—"}</td>
                      <td className={`mono ${delta !== null ? (delta >= 0 ? "delta-pos" : "delta-neg") : ""}`}>
                        {delta !== null ? fmtPct(delta) : "—"}
                      </td>
                    </tr>

                    {/* Master-Detail Accordion Body */}
                    {isExpanded && (
                      <tr>
                        <td colSpan={8} style={{ padding: "12px 16px", background: "#0e1117", borderBottom: "2px solid var(--border)" }}>
                          <div className="grid cols-2" style={{ gap: 16 }}>
                            {/* Live Trades on this date */}
                            <div>
                              <h4 style={{ margin: "0 0 6px 0", fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase" }}>
                                Live Signals ({day})
                              </h4>
                              {l?.trades && l.trades.length > 0 ? (
                                <table style={{ fontSize: 11 }}>
                                  <thead>
                                    <tr>
                                      <th>Symbol</th>
                                      <th>Side</th>
                                      <th>Entry</th>
                                      <th>Exit</th>
                                      <th>Return %</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {l.trades.map((t, idx) => (
                                      <tr key={idx}>
                                        <td className="mono">{t.symbol}</td>
                                        <td className={t.side?.toUpperCase().startsWith("B") ? "side-buy" : "side-sell"}>{t.side}</td>
                                        <td className="mono">{fmtPrice(t.entry_price)}</td>
                                        <td className="mono">{fmtPrice(t.exit_price)}</td>
                                        <td className={`mono ${pnlClass(t.pnl_pct)}`}>{fmtPct(t.pnl_pct)}</td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              ) : (
                                <div className="empty-state" style={{ padding: 8 }}>Zero live signals executed.</div>
                              )}
                            </div>

                            {/* Backtest Trades on this date */}
                            <div>
                              <h4 style={{ margin: "0 0 6px 0", fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase" }}>
                                Backtest Trades ({day})
                              </h4>
                              {b?.trades && b.trades.length > 0 ? (
                                <table style={{ fontSize: 11 }}>
                                  <thead>
                                    <tr>
                                      <th>Symbol</th>
                                      <th>Side</th>
                                      <th>Entry</th>
                                      <th>Exit</th>
                                      <th>Return %</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {b.trades.map((t, idx) => (
                                      <tr key={idx}>
                                        <td className="mono">{t.symbol}</td>
                                        <td className={t.side?.toUpperCase().startsWith("B") ? "side-buy" : "side-sell"}>{t.side}</td>
                                        <td className="mono">{fmtPrice(t.entry_price)}</td>
                                        <td className="mono">{fmtPrice(t.exit_price)}</td>
                                        <td className={`mono ${pnlClass(t.pnl_pct)}`}>{fmtPct(t.pnl_pct)}</td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              ) : (
                                <div className="empty-state" style={{ padding: 8 }}>Zero backtest trades executed.</div>
                              )}
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
          {allDates.length === 0 && (
            <div className="empty-state">No overlapping day records found between live execution and backtest runs.</div>
          )}
        </div>
      </div>
    </Layout>
  );
}