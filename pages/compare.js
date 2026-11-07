import React, { useEffect, useState } from "react";
import Layout from "../components/Layout";
import { apiGet, fmtPct, fmtPrice, fmtTime, pnlClass } from "../lib/api";
import GateReadout from "../components/GateReadout";
import FullComparisonTable from "../components/FullComparisonTable";

function StatCard({ label, value, cls }) {
  return (
    <div>
      <div className={`stat-value ${cls || ""}`}>{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

// 2026-11-03: "Model State Comparison" — the owner's explicit ask was to
// compare both models "for the signal generation hour (volatility,
// strict entry)", not only for hours that happened to produce an
// executed trade (most hours don't — that's the normal, waiting-for-
// retrace case). Hits GET /api/compare/gate-state, the trade-free
// counterpart to /api/compare/trade-alignment.
function ModelStateComparison() {
  const [wfRuns, setWfRuns] = useState(null);
  const [symbol, setSymbol] = useState("BTC");
  const [runId, setRunId] = useState("");
  const [timestamp, setTimestamp] = useState(() => new Date().toISOString());
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/runs", { run_type: "walkforward", limit: 30 })
      .then((d) => {
        if (cancelled) return;
        setWfRuns(d.runs || []);
        const completed = (d.runs || []).find((r) => r.status === "completed");
        if (completed) setRunId(String(completed.id));
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  function setPreset(hoursAgo) {
    setTimestamp(new Date(Date.now() - hoursAgo * 3600 * 1000).toISOString());
  }

  async function runCompare() {
    setLoading(true);
    setError(null);
    try {
      const params = { symbol, timestamp };
      if (runId) params.run_id = runId;
      const res = await apiGet("/compare/gate-state", params);
      setResult(res);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  const liveFired = result?.live_diagnostics?.signal_fired;
  const backtestFired = result?.backtest_diagnostics?.signal_fired;

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10 }}>
        <h3 style={{ margin: 0 }}>Model State Comparison — Signal Generation Hour</h3>
        <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
          Compares BOTH models&apos; volatility/strict-entry gate state for one hour — including hours with no trade at all
        </span>
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 12 }}>
        <label>
          Symbol<br />
          <select className="dropdown-select" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            <option value="BTC">BTC</option>
            <option value="ETH">ETH</option>
          </select>
        </label>

        <label style={{ flex: "1 1 260px", minWidth: 220 }}>
          Hour (ISO, UTC)<br />
          <input
            type="text"
            value={timestamp}
            onChange={(e) => setTimestamp(e.target.value)}
            style={{ width: "100%", padding: "8px 10px", background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)", fontFamily: "IBM Plex Mono, monospace", fontSize: 12 }}
          />
        </label>

        <div style={{ display: "flex", gap: 4, paddingBottom: 1 }}>
          {[["Now", 0], ["-1h", 1], ["-6h", 6], ["-24h", 24]].map(([lbl, h]) => (
            <button key={lbl} type="button" className="btn" style={{ padding: "8px 10px", fontSize: 11 }} onClick={() => setPreset(h)}>
              {lbl}
            </button>
          ))}
        </div>

        <label>
          Backtest Run<br />
          <select className="dropdown-select" value={runId} onChange={(e) => setRunId(e.target.value)}>
            <option value="">— none (live only) —</option>
            {wfRuns?.map((r) => (
              <option key={r.id} value={r.id}>#{r.id} · {fmtTime(r.finished_at)}</option>
            ))}
          </select>
        </label>

        <button type="button" className="btn primary" style={{ padding: "9px 20px" }} onClick={runCompare} disabled={loading}>
          {loading ? "Comparing…" : "Compare"}
        </button>
      </div>

      {error && <div className="banner error">{error}</div>}

      {result && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
            <div style={{ background: "var(--bg)", padding: "12px 14px", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
              <div style={{ color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase", marginBottom: 6 }}>
                Backtest Model {runId ? `— Run #${runId}` : ""}
              </div>
              <GateReadout diag={result.backtest_diagnostics} emptyLabel={runId ? "No backtest diagnostic row within 90m of this hour." : "Pick a backtest run above to compare."} />
            </div>
            <div style={{ background: "var(--bg)", padding: "12px 14px", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
              <div style={{ color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase", marginBottom: 6 }}>
                Live Engine
              </div>
              <GateReadout diag={result.live_diagnostics} emptyLabel="No live diagnostic row within 90m of this hour." />
            </div>
          </div>

          {result.backtest_diagnostics && result.live_diagnostics && (
            <div style={{ marginTop: 10, paddingTop: 8, borderTop: "1px solid var(--border)", fontSize: 12, color: "var(--accent-secondary)" }}>
              {backtestFired && !liveFired
                ? "Backtest fired here; live did not — the gate readouts above show exactly which gate diverged."
                : !backtestFired && liveFired
                ? "Live fired here; the backtest's own params did not clear both gates at this hour."
                : backtestFired === liveFired
                ? "Both models agree on this hour's outcome."
                : null}
            </div>
          )}

          {/* 2026-11-06: full field-by-field comparison with a variance
              column — the summary readouts above only show gate PASS/FAIL;
              this table is for actually pinpointing a divergence: every
              raw number and training window each model used, side by
              side, so a discrepancy (or its absence) is visible directly
              rather than inferred from gate outcomes alone. */}
          <FullComparisonTable backtest={result.backtest_diagnostics} live={result.live_diagnostics} />
        </>
      )}
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

      <ModelStateComparison />

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