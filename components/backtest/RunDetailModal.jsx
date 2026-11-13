import React, { useState, useMemo } from "react";
import EquityCurveChart from "../EquityCurveChart";
import { apiGet, fmtMoney, fmtPct, fmtPrice, fmtTime, pnlClass } from "../../lib/api";
import GateReadout from "../GateReadout";
import FullComparisonTable from "../FullComparisonTable";

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

// Numeric form of the same calc as calculateTradeDrawdown() below, so the
// sort control (2026-11-03) can rank trades by drawdown risk without
// re-parsing the formatted "-X.XX%" string.
function tradeDrawdownPct(tr) {
  if (!tr.entry_price || !tr.sl_price) return null;
  try {
    const isBuy = (tr.side || "").toUpperCase().startsWith("B");
    const diff = isBuy ? tr.entry_price - tr.sl_price : tr.sl_price - tr.entry_price;
    return Math.abs((diff / tr.entry_price) * 100);
  } catch {
    return null;
  }
}

function calculateTradeDrawdown(tr) {
  const pct = tradeDrawdownPct(tr);
  return pct === null ? "—" : `-${pct.toFixed(2)}%`;
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

export default function RunDetailModal({ modal, onClose, bounds }) {
  const [modalView, setModalView] = useState("overview"); // 'overview' | 'trades'
  const [alignedTrade, setAlignedTrade] = useState(null);
  const [alignLoading, setAlignLoading] = useState(false);
  // 2026-11-03: Executed Trades sort control. Defaults to most-recent-first
  // per explicit request (the API returns rows oldest-first, ORDER BY id ASC,
  // which is the right order for backtest replay but the wrong one to read).
  const [tradeSort, setTradeSort] = useState("recent");

  const sortedTrades = useMemo(() => {
    const trades = modal?.trades || [];
    const arr = [...trades];
    switch (tradeSort) {
      case "oldest":
        arr.sort((a, b) => new Date(a.entry_ts) - new Date(b.entry_ts));
        break;
      case "return_desc":
        arr.sort((a, b) => (b.pnl_pct ?? -Infinity) - (a.pnl_pct ?? -Infinity));
        break;
      case "return_asc":
        arr.sort((a, b) => (a.pnl_pct ?? Infinity) - (b.pnl_pct ?? Infinity));
        break;
      case "drawdown_desc":
        arr.sort((a, b) => (tradeDrawdownPct(b) ?? -Infinity) - (tradeDrawdownPct(a) ?? -Infinity));
        break;
      case "recent":
      default:
        arr.sort((a, b) => new Date(b.entry_ts) - new Date(a.entry_ts));
        break;
    }
    return arr;
  }, [modal?.trades, tradeSort]);

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
        run_id: modal.runId,
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

    // 2026-11-08: prefer the TRUE test-window boundaries the backtest
    // itself walked (walkforward.py's resolved_start_date/end_date,
    // from the actual first/last bar of test data) over inferring from
    // trade timestamps below. The backtest always walks fresh data
    // through to its real end (data_download.py refetches every run,
    // no end= bound) — but a duration-preset run's last TRADE can exit
    // well before that if the tail of the window had no closing trade,
    // which previously made End Date look stale even though the data
    // behind it wasn't. See PLAN.md's 2026-11-08 entry.
    const resolved = modal.run.metrics || {};
    if (!startDate) startDate = resolved.resolved_start_date || null;
    if (!endDate) endDate = resolved.resolved_end_date || null;

    if (trades.length > 0) {
      if (!startDate) startDate = trades[0].entry_ts?.slice(0, 10);
      if (!endDate) endDate = trades[trades.length - 1].exit_ts?.slice(0, 10);
    }

    // 2026-11-02: an explicit start_date with NO end_date means "run
    // through the latest available data" (BacktestForm.jsx's "Latest
    // Available" placeholder; walkforward.py resolves END_DATE=None to
    // the dataset's last bar) — that's a real, resolved end date, not a
    // missing one. Previously this only ever got filled in from a
    // persisted trade's exit_ts, so any run where per-trade persistence
    // failed (see record_backtest_trades's 2026-11-02 fix) showed End
    // Date AND Test Window blank even though the run itself completed
    // fine with a real, known end date.
    if (!endDate && startDate && bounds?.latest_date) {
      endDate = bounds.latest_date;
    }

    if (!startDate && modal.run.finished_at && durationDays) {
      const finDate = new Date(modal.run.finished_at);
      const stDate = new Date(finDate.getTime() - durationDays * 24 * 3600 * 1000);
      startDate = stDate.toISOString().slice(0, 10);
      endDate = finDate.toISOString().slice(0, 10);
    }

    // Date-range runs (as opposed to a duration preset) never set
    // walk_forward_days, so Test Window used to show "—" even once
    // start/end date were both known — compute it from the resolved
    // dates instead.
    if (!durationDays && startDate && endDate) {
      const days = Math.round((new Date(endDate) - new Date(startDate)) / (24 * 3600 * 1000));
      if (!isNaN(days) && days > 0) durationDays = days;
    }

    const execDuration = calculateExecutionDuration(modal.run.started_at, modal.run.finished_at);

    return {
      startDate: startDate || "—",
      endDate: endDate || "—",
      durationDays: durationDays ? `${durationDays} Days` : "—",
      execDuration,
    };
  }, [modal, bounds]);

  if (!modal) return null;

  return (
    <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-card">
        <button className="modal-close" onClick={onClose} aria-label="Close">&times;</button>

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 14, gap: 14, flexWrap: "wrap" }}>
          <div>
            <h3 style={{ margin: 0 }}>Run #{modal.runId}</h3>
            <div className="modal-sub" style={{ margin: 0, marginTop: 4 }}>
              {modal.loading
                ? "Loading simulation telemetry…"
                : modal.error
                ? "Failed to fetch run data"
                : `${modal.run.run_type?.toUpperCase()} · ${modal.run.status?.toUpperCase()} · Finished ${fmtTime(modal.run.finished_at)}`}
            </div>
          </div>

          {/* 2026-11-12 (owner UI request): a segmented "dynamic island"
              tab control in place of having to click the Total Trades
              row or the bottom "Inspect All Executed Trades" button —
              those still work (left as alternate entry points) but this
              is the primary, always-visible way to switch views. */}
          {!modal.loading && !modal.error && modal.run && (
            <div
              role="tablist"
              aria-label="Run detail view"
              style={{
                display: "inline-flex", padding: 3, borderRadius: 999,
                background: "var(--bg)", border: "1px solid var(--border-soft)",
              }}
            >
              {[
                { key: "overview", label: "Overview" },
                { key: "trades", label: `Trades (${modal.trades?.length || 0})` },
              ].map((tab) => (
                <button
                  key={tab.key}
                  type="button"
                  role="tab"
                  aria-selected={modalView === tab.key}
                  onClick={() => { setModalView(tab.key); if (tab.key === "overview") setAlignedTrade(null); }}
                  style={{
                    padding: "6px 16px", fontSize: 12, fontWeight: 600, borderRadius: 999,
                    border: "none", cursor: "pointer", transition: "background 0.15s, color 0.15s",
                    background: modalView === tab.key ? "var(--accent-secondary)" : "transparent",
                    color: modalView === tab.key ? "#FFF" : "var(--text-muted)",
                  }}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          )}
        </div>

        {modal.loading && <div className="empty-state">Loading run records…</div>}
        {modal.error && <div className="modal-error">{modal.error}</div>}

        {!modal.loading && !modal.error && modal.run && (
          <>
            {modalView === "overview" && (
              <>
                <EquityCurveChart
                  trades={modal.trades}
                  curve={modal.curve}
                  runMetrics={modal.run.metrics || {}}
                  startDate={runAnalytics.startDate !== "—" ? runAnalytics.startDate : null}
                  endDate={runAnalytics.endDate !== "—" ? runAnalytics.endDate : null}
                  initialCapital={10.0}
                />

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
                    {/* 2026-11-12 (owner UI request): Initial Capital / Avg Daily
                        Trade Freq / Avg Daily Return — walkforward.py's
                        compute_metrics() call site now emits these (see its
                        2026-11-12 comment); older runs predate them and show
                        "—" via the same fallback every metric here already uses. */}
                    <div style={{ background: "var(--bg)", padding: "10px 14px", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
                      <div style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase" }}>Initial Capital</div>
                      <div className="mono" style={{ fontSize: 13, fontWeight: 600 }}>
                        {modal.run.metrics?.["Initial Capital ($)"] ? fmtMoney(parseFloat(modal.run.metrics["Initial Capital ($)"])) : "—"}
                      </div>
                    </div>
                    <div style={{ background: "var(--bg)", padding: "10px 14px", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
                      <div style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase" }}>Final Capital</div>
                      <div className="mono" style={{ fontSize: 13, fontWeight: 600 }}>
                        {modal.run.metrics?.["Final Capital ($)"] ? fmtMoney(parseFloat(modal.run.metrics["Final Capital ($)"])) : "—"}
                      </div>
                    </div>
                    <div style={{ background: "var(--bg)", padding: "10px 14px", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
                      <div style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase" }}>Avg Daily Trade Freq</div>
                      <div className="mono" style={{ fontSize: 13, fontWeight: 600 }}>
                        {modal.run.metrics?.["Avg Daily Trade Freq"] ?? "—"}{modal.run.metrics?.["Avg Daily Trade Freq"] ? "/day" : ""}
                      </div>
                    </div>
                    <div style={{ background: "var(--bg)", padding: "10px 14px", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
                      <div style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase" }}>Avg Daily Return</div>
                      <div
                        className="mono"
                        style={{ fontSize: 13, fontWeight: 600, color: parseFloat(modal.run.metrics?.["Avg Daily Return (%)"]) < 0 ? "var(--red)" : "var(--green)" }}
                      >
                        {modal.run.metrics?.["Avg Daily Return (%)"] ? `${modal.run.metrics["Avg Daily Return (%)"]}%` : "—"}
                      </div>
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
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8, flexWrap: "wrap", gap: 8 }}>
                  <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                    Total Executions: {modal.trades?.length || 0} Trades | Click &quot;Compare to Live&quot; on any trade to inspect model decisions
                  </span>
                  <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--text-muted)" }}>
                    Sort by
                    <select
                      className="dropdown-select"
                      value={tradeSort}
                      onChange={(e) => setTradeSort(e.target.value)}
                      style={{ padding: "4px 8px", fontSize: 12 }}
                    >
                      <option value="recent">Most Recent First</option>
                      <option value="oldest">Oldest First</option>
                      <option value="return_desc">Return % (High → Low)</option>
                      <option value="return_asc">Return % (Low → High)</option>
                      <option value="drawdown_desc">Drawdown Risk (High → Low)</option>
                    </select>
                  </label>
                </div>
                {/* 2026-11-03: volatility was requested as a filter dimension too, but
                    it isn't a per-trade field — it's a per-bar diagnostic
                    (pyquant_backtest_diagnostics), sampled continuously, not just at
                    entry. Sorting trades by it would mean picking one arbitrary bar
                    (e.g. entry-tick) per trade, which could be misleading. Use
                    "Compare to Live" on a trade below, or the Compare page's Model
                    State Comparison, to see a trade's actual vol/strict gate state. */}
                <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 8 }}>
                  No volatility column here — volatility is a per-bar gate reading, not a per-trade value. Use &quot;Compare to Live&quot; on a trade to see its vol/strict gate state at entry.
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
                        {sortedTrades.map((tr, idx) => {
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
                                        <>
                                          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                                            <div>
                                              <div style={{ color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase", marginBottom: 4 }}>
                                                Backtest Model — Executed Trade
                                              </div>
                                              <div>Entry Price: <span className="mono">{fmtPrice(tr.entry_price)}</span></div>
                                              <div>Exit Price: <span className="mono">{fmtPrice(tr.exit_price)}</span></div>
                                              <div>Stop Loss: <span className="mono">{fmtPrice(tr.sl_price)}</span></div>
                                              <div>Take Profit: <span className="mono">{fmtPrice(tr.tp_price)}</span></div>
                                              <div>Realized Return: <span className={`mono ${pnlClass(tr.pnl_pct)}`}>{fmtPct(tr.pnl_pct)}</span></div>
                                              {/* 2026-11-03: the backtest's OWN gate state at this bar — see
                                                  pyquant_state.record_backtest_diagnostics(). Previously this
                                                  column showed nothing but price/PnL, so there was no way to see
                                                  WHY the backtest fired here beyond "it did" — now it shows the
                                                  same vol/strict gate numbers live's column does, for a real
                                                  apples-to-apples comparison. */}
                                              <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px dashed var(--border)" }}>
                                                <GateReadout diag={alignedTrade.data.backtest_diagnostics} />
                                              </div>
                                            </div>

                                            <div>
                                              <div style={{ color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase", marginBottom: 4 }}>
                                                Live Engine — Evaluated Tick
                                              </div>
                                              <GateReadout diag={alignedTrade.data.live_diagnostics} />

                                              {alignedTrade.data.live_signal && (
                                                <div style={{ marginTop: 6, borderTop: "1px dashed var(--border)", paddingTop: 4 }}>
                                                  Matching Live Signal #{alignedTrade.data.live_signal.id}:{" "}
                                                  <span className="mono">{alignedTrade.data.live_signal.side} @ {fmtPrice(alignedTrade.data.live_signal.entry_price)}</span>{" "}
                                                  ({fmtPct(alignedTrade.data.live_signal.realized_pnl_pct)})
                                                </div>
                                              )}
                                            </div>
                                          </div>

                                          {/* 2026-11-06: full field-by-field comparison with a variance
                                              column — the panels above only show gate PASS/FAIL; this is
                                              for actually pinpointing a divergence (every raw price/pred/
                                              band value and each model's training window, side by side). */}
                                          <FullComparisonTable
                                            backtest={alignedTrade.data.backtest_diagnostics}
                                            live={alignedTrade.data.live_diagnostics}
                                          />

                                          {alignedTrade.data.backtest_diagnostics && !alignedTrade.data.live_diagnostics?.signal_fired && (
                                            <div style={{ marginTop: 10, paddingTop: 8, borderTop: "1px solid var(--border)", fontSize: 12, color: "var(--accent-secondary)" }}>
                                              {alignedTrade.data.backtest_diagnostics.signal_fired
                                                ? "The backtest fired here; live's own gate state at this hour did not — the gap above is why."
                                                : "Neither model's recorded gate state shows a clean fire at this exact tick — the backtest trade may have been decided on an adjacent bar."}
                                            </div>
                                          )}
                                        </>
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