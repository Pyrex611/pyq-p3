import React, { useState, useMemo } from "react";
import EquityCurveChart from "../EquityCurveChart";
import { apiGet, fmtMoney, fmtPct, fmtPrice, fmtTime, pnlClass } from "../../lib/api";

function formatHoldDuration(entryTs, exitTs) {
  if (!entryTs || !exitTs) return "—";
  try {
    const ms = new Date(exitTs) - new Date(entryTs);
    if (isNaN(ms) || ms < 0) return "—";
    const totalMinutes = Math.floor(ms / (1000 * 60));
    if (totalMinutes < 60) return `${totalMinutes}m`;
    const hours = Math.floor(totalMinutes / 60);
    const mins = totalMinutes % 60;
    return `${hours}h ${mins}m`;
  } catch {
    return "—";
  }
}

function calculateTradeDrawdown(tr) {
  if (!tr.entry_price || !tr.sl_price) return "—";
  try {
    const isBuy = (tr.side || "").toUpperCase().startsWith("B");
    const diff = isBuy ? tr.entry_price - tr.sl_price : tr.sl_price - tr.entry_price;
    const maxLossRisk = (diff / tr.entry_price) * 100;
    return `-${Math.abs(maxLossRisk).toFixed(2)}%`;
  } catch {
    return "—";
  }
}

function calculateExecutionDuration(startedAt, finishedAt) {
  if (!startedAt || !finishedAt) return "—";
  try {
    const ms = new Date(finishedAt) - new Date(startedAt);
    if (isNaN(ms) || ms <= 0) return "—";
    const totalSeconds = Math.floor(ms / 1000);
    const mins = Math.floor(totalSeconds / 60);
    const secs = totalSeconds % 60;
    return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
  } catch {
    return "—";
  }
}

export default function RunDetailModal({ modal, onClose }) {
  const [modalView, setModalView] = useState("overview"); // 'overview' | 'trades'
  const [alignedTrade, setAlignedTrade] = useState(null);
  const [alignLoading, setAlignLoading] = useState(false);

  async function compareTradeToLive(tr) {
    if (alignedTrade?.tradeId === tr.id) {
      setAlignedTrade(null);
      return;
    }
    setAlignLoading(true);
    setAlignedTrade({ tradeId: tr.id, loading: true, data: null });
    try {
      const res = await apiGet("/compare/trade-alignment", {
        symbol: tr.symbol,
        timestamp: tr.entry_ts,
      });
      setAlignedTrade({ tradeId: tr.id, loading: false, data: res });
    } catch (err) {
      setAlignedTrade({ tradeId: tr.id, loading: false, error: err.message });
    } finally {
      setAlignLoading(false);
    }
  }

  const runAnalytics = useMemo(() => {
    if (!modal?.run) return {};
    const params = modal.params || {};
    const trades = modal.trades || [];

    let startDate = params.start_date || null;
    let endDate = params.end_date || null;
    let durationDays = params.walk_forward_days || null;

    if (trades.length > 0) {
      if (!startDate) startDate = trades[0].entry_ts?.slice(0, 10);
      if (!endDate) endDate = trades[trades.length - 1].exit_ts?.slice(0, 10);
    }

    if (!startDate && modal.run.finished_at && durationDays) {
      const finDate = new Date(modal.run.finished_at);
      const stDate = new Date(finDate.getTime() - durationDays * 24 * 3600 * 1000);
      startDate = stDate.toISOString().slice(0, 10);
      endDate = finDate.toISOString().slice(0, 10);
    }

    const execDuration = calculateExecutionDuration(modal.run.started_at, modal.run.finished_at);

    return {
      startDate: startDate || "—",
      endDate: endDate || "—",
      durationDays: durationDays ? `${durationDays} Days` : "—",
      execDuration,
    };
  }, [modal]);

  if (!modal) return null;

  return (
    <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-card">
        <button className="modal-close" onClick={onClose} aria-label="Close">&times;</button>

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 14 }}>
          <div>
            <h3 style={{ margin: 0 }}>
              Run #{modal.runId} {modalView === "trades" ? "— Executed Trades Log" : "Analytics"}
            </h3>
            <div className="modal-sub" style={{ margin: 0, marginTop: 4 }}>
              {modal.loading
                ? "Loading simulation telemetry…"
                : modal.error
                ? "Failed to fetch run data"
                : `${modal.run.run_type?.toUpperCase()} · ${modal.run.status?.toUpperCase()} · Finished ${fmtTime(modal.run.finished_at)}`}
            </div>
          </div>

          {modalView === "trades" && (
            <button
              type="button"
              className="btn"
              style={{ padding: "5px 12px", fontSize: 12 }}
              onClick={() => { setModalView("overview"); setAlignedTrade(null); }}
            >
              ← Back to Overview
            </button>
          )}
        </div>

        {modal.loading && <div className="empty-state">Loading run records…</div>}
        {modal.error && <div className="modal-error">{modal.error}</div>}

        {!modal.loading && !modal.error && modal.run && (
          <>
            {modalView === "overview" && (
              <>
                <EquityCurveChart trades={modal.trades} runMetrics={modal.run.metrics || {}} initialCapital={10.0} />

                <div className="modal-section" style={{ marginTop: 16 }}>
                  <h4>Backtest Window & Runtime</h4>
                  <div className="grid cols-4" style={{ gap: 10 }}>
                    <div style={{ background: "var(--bg)", padding: "10px 14px", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
                      <div style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase" }}>Start Date</div>
                      <div className="mono" style={{ fontSize: 13, fontWeight: 600 }}>{runAnalytics.startDate}</div>
                    </div>
                    <div style={{ background: "var(--bg)", padding: "10px 14px", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
                      <div style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase" }}>End Date</div>
                      <div className="mono" style={{ fontSize: 13, fontWeight: 600 }}>{runAnalytics.endDate}</div>
                    </div>
                    <div style={{ background: "var(--bg)", padding: "10px 14px", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
                      <div style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase" }}>Test Window</div>
                      <div className="mono" style={{ fontSize: 13, fontWeight: 600 }}>{runAnalytics.durationDays}</div>
                    </div>
                    <div style={{ background: "var(--bg)", padding: "10px 14px", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
                      <div style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase" }}>Execution Time</div>
                      <div className="mono" style={{ fontSize: 13, fontWeight: 600 }}>{runAnalytics.execDuration}</div>
                    </div>
                  </div>
                </div>

                <div className="modal-section">
                  <h4>Performance Metrics Summary</h4>
                  <div className="kv-grid">
                    {Object.entries(modal.run.metrics || {}).map(([k, v]) => {
                      const isTotalTrades = k === "Total Trades" || k === "trade_count";
                      return (
                        <div
                          key={k}
                          className="kv-row"
                          style={isTotalTrades ? { background: "var(--accent)", borderRadius: 4, padding: "6px 10px", cursor: "pointer", border: "1px solid var(--accent-secondary)" } : {}}
                          onClick={() => isTotalTrades && setModalView("trades")}
                        >
                          <span className="kv-key" style={isTotalTrades ? { color: "#FFF", fontWeight: 600 } : {}}>
                            {k} {isTotalTrades ? "➔ (Inspect Trades)" : ""}
                          </span>
                          <span className="kv-val" style={isTotalTrades ? { color: "#FFF", fontWeight: 700 } : {}}>
                            {String(v ?? "—")}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </div>

                <div className="modal-section">
                  <h4>Strategy Parameters</h4>
                  <div className="kv-grid">
                    {Object.entries(modal.params || {}).map(([k, v]) => (
                      <div key={k} className="kv-row">
                        <span className="kv-key">{k}</span>
                        <span className="kv-val">{String(v ?? "—")}</span>
                      </div>
                    ))}
                  </div>
                </div>

                <div style={{ display: "flex", justifyContent: "center", marginTop: 14 }}>
                  <button
                    type="button"
                    className="btn primary"
                    style={{ padding: "9px 26px", fontSize: 13 }}
                    onClick={() => setModalView("trades")}
                  >
                    Inspect All Executed Trades ({modal.trades?.length || 0}) →
                  </button>
                </div>
              </>
            )}

            {modalView === "trades" && (
              <div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                  <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                    Total Executions: {modal.trades?.length || 0} Trades | Click &quot;Compare to Live&quot; on any trade to inspect model decisions
                  </span>
                </div>

                {modal.trades && modal.trades.length > 0 ? (
                  <div style={{ maxHeight: 520, overflowY: "auto", border: "1px solid var(--border-soft)", borderRadius: 6 }}>
                    <table style={{ fontSize: 12 }}>
                      <thead>
                        <tr>
                          <th>Symbol</th>
                          <th>Side</th>
                          <th>Entry Time</th>
                          <th>Exit Time</th>
                          <th>Duration</th>
                          <th>Drawdown Risk</th>
                          <th>Return (%)</th>
                          <th>Exit Leg</th>
                          <th style={{ textAlign: "right" }}>Live Telemetry</th>
                        </tr>
                      </thead>
                      <tbody>
                        {modal.trades.map((tr, idx) => {
                          const isSelected = alignedTrade?.tradeId === (tr.id || idx);
                          return (
                            <React.Fragment key={tr.id || idx}>
                              <tr>
                                <td className="mono" style={{ fontWeight: 600 }}>{tr.symbol}</td>
                                <td className={tr.side?.toUpperCase().startsWith("B") ? "side-buy" : "side-sell"}>
                                  {tr.side}
                                </td>
                                <td className="mono" style={{ fontSize: 11 }}>{fmtTime(tr.entry_ts)}</td>
                                <td className="mono" style={{ fontSize: 11 }}>{fmtTime(tr.exit_ts)}</td>
                                <td className="mono">{formatHoldDuration(tr.entry_ts, tr.exit_ts)}</td>
                                <td className="mono" style={{ color: "var(--red)" }}>{calculateTradeDrawdown(tr)}</td>
                                <td className={`mono ${pnlClass(tr.pnl_pct)}`}>{fmtPct(tr.pnl_pct)}</td>
                                <td>
                                  <span className={`pill ${tr.exit_leg === "TP" ? "closed_tp" : tr.exit_leg === "SL" ? "closed_sl" : "open"}`}>
                                    {tr.exit_leg || "CLOSE"}
                                  </span>
                                </td>
                                <td style={{ textAlign: "right" }}>
                                  <button
                                    type="button"
                                    className={`btn ${isSelected ? "primary" : ""}`}
                                    style={{ padding: "4px 9px", fontSize: 11 }}
                                    onClick={() => compareTradeToLive(tr)}
                                  >
                                    {isSelected ? "Hide Alignment" : "Compare to Live"}
                                  </button>
                                </td>
                              </tr>

                              {isSelected && (
                                <tr>
                                  <td colSpan={9} style={{ padding: "0 10px 10px 10px", background: "var(--bg)" }}>
                                    <div className="trade-compare-drawer">
                                      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                                        <span style={{ fontWeight: 600, color: "var(--accent-secondary)" }}>
                                          Live Model Alignment @ {fmtTime(tr.entry_ts)}
                                        </span>
                                        {alignLoading && <span className="mono">Querying historical database…</span>}
                                      </div>

                                      {alignedTrade.error && (
                                        <div style={{ color: "var(--red)" }}>Error querying live telemetry: {alignedTrade.error}</div>
                                      )}

                                      {alignedTrade.data && (
                                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                                          <div>
                                            <div style={{ color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase", marginBottom: 4 }}>
                                              Simulated Backtest Trade
                                            </div>
                                            <div>Entry Price: <span className="mono">{fmtPrice(tr.entry_price)}</span></div>
                                            <div>Exit Price: <span className="mono">{fmtPrice(tr.exit_price)}</span></div>
                                            <div>Stop Loss: <span className="mono">{fmtPrice(tr.sl_price)}</span></div>
                                            <div>Take Profit: <span className="mono">{fmtPrice(tr.tp_price)}</span></div>
                                            <div>Realized Return: <span className={`mono ${pnlClass(tr.pnl_pct)}`}>{fmtPct(tr.pnl_pct)}</span></div>
                                          </div>

                                          <div>
                                            <div style={{ color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase", marginBottom: 4 }}>
                                              Live Engine Telemetry (Evaluated Tick)
                                            </div>
                                            {alignedTrade.data.live_diagnostics ? (
                                              <>
                                                <div>Trend Decision: <span className="mono">{alignedTrade.data.live_diagnostics.trend?.toUpperCase()}</span></div>
                                                <div>
                                                  Vol Gate:{" "}
                                                  <span style={{ color: alignedTrade.data.live_diagnostics.vol_gate_passed ? "var(--green)" : "var(--amber)" }}>
                                                    {alignedTrade.data.live_diagnostics.vol_gate_passed ? "PASSED" : "BLOCKED"}
                                                  </span>{" "}
                                                  ({((alignedTrade.data.live_diagnostics.pct_diff || 0) * 100).toFixed(3)}% vs{" "}
                                                  {((alignedTrade.data.live_diagnostics.vol_gate_threshold || 0) * 100).toFixed(3)}%)
                                                </div>
                                                <div>
                                                  Strict Entry Gate:{" "}
                                                  <span style={{ color: alignedTrade.data.live_diagnostics.strict_gate_passed ? "var(--green)" : "var(--amber)" }}>
                                                    {alignedTrade.data.live_diagnostics.strict_gate_passed ? "PASSED" : "BLOCKED"}
                                                  </span>{" "}
                                                  ({((alignedTrade.data.live_diagnostics.strict_diff || 0) * 100).toFixed(3)}% vs{" "}
                                                  {((alignedTrade.data.live_diagnostics.strict_threshold || 0) * 100).toFixed(3)}%)
                                                </div>
                                                <div>
                                                  Live Signal Fired:{" "}
                                                  <span style={{ fontWeight: 600, color: alignedTrade.data.live_diagnostics.signal_fired ? "var(--green)" : "var(--text-faint)" }}>
                                                    {alignedTrade.data.live_diagnostics.signal_fired ? "YES" : "NO"}
                                                  </span>
                                                </div>
                                              </>
                                            ) : (
                                              <div style={{ color: "var(--text-muted)" }}>
                                                No live diagnostic record within 90m window of this historical trade.
                                              </div>
                                            )}

                                            {alignedTrade.data.live_signal && (
                                              <div style={{ marginTop: 6, borderTop: "1px dashed var(--border)", paddingTop: 4 }}>
                                                Matching Live Signal #{alignedTrade.data.live_signal.id}:{" "}
                                                <span className="mono">{alignedTrade.data.live_signal.side} @ {fmtPrice(alignedTrade.data.live_signal.entry_price)}</span>{" "}
                                                ({fmtPct(alignedTrade.data.live_signal.realized_pnl_pct)})
                                              </div>
                                            )}
                                          </div>
                                        </div>
                                      )}
                                    </div>
                                  </td>
                                </tr>
                              )}
                            </React.Fragment>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <div className="empty-state">
                    No individual trade records found in database for this run (run predates per-trade logging).
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}