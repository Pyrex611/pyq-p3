import { useEffect, useRef, useState, useMemo } from "react";
import Layout from "../components/Layout";
import EquityCurveChart from "../components/EquityCurveChart";
import { apiGet, apiPost, fmtMoney, fmtPct, fmtPrice, fmtTime, pnlClass } from "../lib/api";

const OPTUNA_TRIAL_OPTIONS = [10, 20, 30, 50, 70, 100, 150, 200, 250, 300, 400, 500];
const LEVERAGE_OPTIONS = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50];

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

export default function Backtest() {
  const [wfRuns, setWfRuns] = useState(null);
  const [optRuns, setOptRuns] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [bounds, setBounds] = useState(null);

  const [form, setForm] = useState({
    walk_forward_days: "15",
    start_date: "",
    end_date: "",
    bayesian_trials: "150",
    leverage: "10",
    use_bayesian: true,
    use_atr_sl: true,
  });

  const [statusText, setStatusText] = useState("");
  const [kaggleUrl, setKaggleUrl] = useState(null);
  const [modal, setModal] = useState(null);
  const [showTradesList, setShowTradesList] = useState(false);
  const [alignedTrade, setAlignedTrade] = useState(null);
  const [alignLoading, setAlignLoading] = useState(false);

  const startDateInputRef = useRef(null);
  const endDateInputRef = useRef(null);
  const autoPollTimerRef = useRef(null);

  async function loadRuns() {
    try {
      const wf = await apiGet("/runs", { run_type: "walkforward" });
      setWfRuns(wf.runs);
      const opt = await apiGet("/runs", { run_type: "daily_optimizer" });
      setOptRuns(opt.runs);
      setLoadError(null);
    } catch (e) {
      setLoadError(e.message);
    }
  }

  useEffect(() => {
    loadRuns();
    apiGet("/backtests/bounds")
      .then(setBounds)
      .catch(() => {
        const now = new Date();
        const earliest = new Date(now.getTime() - 365 * 24 * 3600 * 1000);
        setBounds({
          earliest_date: earliest.toISOString().split("T")[0],
          latest_date: now.toISOString().split("T")[0],
        });
      });
  }, []);

  // Autonomous Self-Refreshing Polling Loop
  // Automatically polls runs table every 8 seconds when any run is non-terminal
  useEffect(() => {
    const hasActiveRun =
      (wfRuns && wfRuns.some((r) => r.status === "queued" || r.status === "running")) ||
      (optRuns && optRuns.some((r) => r.status === "queued" || r.status === "running"));

    if (hasActiveRun) {
      if (!autoPollTimerRef.current) {
        autoPollTimerRef.current = setInterval(() => {
          loadRuns();
        }, 8000);
      }
    } else {
      if (autoPollTimerRef.current) {
        clearInterval(autoPollTimerRef.current);
        autoPollTimerRef.current = null;
      }
    }

    return () => {
      if (autoPollTimerRef.current) {
        clearInterval(autoPollTimerRef.current);
        autoPollTimerRef.current = null;
      }
    };
  }, [wfRuns, optRuns]);

  function setPreset(days) {
    setForm({
      ...form,
      walk_forward_days: String(days),
      start_date: "",
      end_date: "",
    });
  }

  function formBody() {
    const body = {};
    if (form.start_date) {
      body.start_date = form.start_date;
      if (form.end_date) body.end_date = form.end_date;
    } else if (form.walk_forward_days) {
      body.walk_forward_days = parseInt(form.walk_forward_days, 10);
    }
    body.bayesian_trials = parseInt(form.bayesian_trials, 10);
    body.leverage = parseFloat(form.leverage);
    body.use_bayesian = !!form.use_bayesian;
    body.use_atr_sl = !!form.use_atr_sl;
    return body;
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setStatusText("Dispatching run to GitHub Actions runner…");
    try {
      const result = await apiPost("/backtests", formBody());
      setStatusText(`Run #${result.run_id} queued — auto-refresh active.`);
      loadRuns();
    } catch (err) {
      setStatusText("Failed to queue backtest: " + err.message);
    }
  }

  async function handleKaggleSync() {
    setKaggleUrl(null);
    setStatusText("Pushing parameters to Kaggle notebook…");
    try {
      const result = await apiPost("/backtests/sync-to-kaggle", formBody());
      setStatusText(`Run #${result.run_id} pushed.`);
      setKaggleUrl(result.kaggle_kernel_url);
      loadRuns();
    } catch (err) {
      setStatusText("Failed to sync to Kaggle: " + err.message);
    }
  }

  async function showDetail(runId) {
    setShowTradesList(false);
    setAlignedTrade(null);
    setModal({ runId, loading: true, run: null, params: {}, error: null, trades: null });
    try {
      const run = await apiGet(`/runs/${runId}`);
      let params = {};
      try {
        params = typeof run.params === "string" ? JSON.parse(run.params) : (run.params || {});
      } catch {}

      let trades = null;
      if (run.run_type === "walkforward") {
        try {
          const t = await apiGet(`/runs/${runId}/trades`);
          if (t.trades && t.trades.length > 0) trades = t.trades;
        } catch {}
      }
      setModal({ runId, loading: false, run, params, error: null, trades });
    } catch (e) {
      setModal({ runId, loading: false, run: null, params: {}, error: e.message, trades: null });
    }
  }

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

  const isAnyRunActive =
    (wfRuns && wfRuns.some((r) => r.status === "queued" || r.status === "running")) ||
    (optRuns && optRuns.some((r) => r.status === "queued" || r.status === "running"));

  return (
    <Layout title="Backtest Studio" footer="PyQuant Backtest Engine — Online Kalman Filter Validation & Parameter Studio">
      {loadError && <div className="banner error">System Connection Error: {loadError}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h3 style={{ margin: 0 }}>Configure Walk-Forward Simulation</h3>
          <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
            Dataset Coverage: {bounds ? `${bounds.earliest_date} → ${bounds.latest_date}` : "Trailing 366 Days"}
          </span>
        </div>

        {/* Quick Presets */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 14, marginBottom: 14 }}>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Duration Presets:</span>
          {["7", "14", "30", "60", "365"].map((d) => (
            <button
              key={d}
              type="button"
              className={`btn ${form.walk_forward_days === d && !form.start_date ? "primary" : ""}`}
              style={{ padding: "4px 10px", fontSize: 11 }}
              onClick={() => setPreset(d)}
            >
              {d === "365" ? "Full Dataset (366D)" : `${d} Days`}
            </button>
          ))}
        </div>

        <form onSubmit={handleSubmit}>
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "flex-end" }}>
            {/* Duration Days */}
            <label>
              Duration Days<br />
              <input
                type="number"
                min="1"
                max="366"
                style={{ width: 95, padding: "7px 10px", background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)" }}
                disabled={!!form.start_date}
                value={form.walk_forward_days}
                onChange={(e) => setForm({ ...form, walk_forward_days: e.target.value })}
              />
            </label>

            {/* Start Date Button Trigger */}
            <label>
              Start Date<br />
              <button
                type="button"
                className={`date-picker-button ${form.start_date ? "active" : ""}`}
                onClick={() => {
                  try { startDateInputRef.current?.showPicker(); }
                  catch { startDateInputRef.current?.focus(); startDateInputRef.current?.click(); }
                }}
              >
                <span>📅</span>
                <span>{form.start_date || "Pick Start Date"}</span>
              </button>
              <input
                ref={startDateInputRef}
                type="date"
                style={{ position: "absolute", opacity: 0, pointerEvents: "none", width: 0, height: 0 }}
                min={bounds?.earliest_date}
                max={form.end_date || bounds?.latest_date}
                value={form.start_date}
                onChange={(e) => setForm({ ...form, start_date: e.target.value, walk_forward_days: "" })}
              />
            </label>

            {/* End Date Button Trigger */}
            <label>
              End Date<br />
              <button
                type="button"
                className={`date-picker-button ${form.end_date ? "active" : ""}`}
                disabled={!form.start_date}
                style={{ opacity: !form.start_date ? 0.45 : 1 }}
                onClick={() => {
                  try { endDateInputRef.current?.showPicker(); }
                  catch { endDateInputRef.current?.focus(); endDateInputRef.current?.click(); }
                }}
              >
                <span>📅</span>
                <span>{form.end_date || "Latest Bar"}</span>
              </button>
              <input
                ref={endDateInputRef}
                type="date"
                style={{ position: "absolute", opacity: 0, pointerEvents: "none", width: 0, height: 0 }}
                min={form.start_date || bounds?.earliest_date}
                max={bounds?.latest_date}
                value={form.end_date}
                onChange={(e) => setForm({ ...form, end_date: e.target.value })}
              />
            </label>

            {/* Optuna Trials Dropdown (Multiples of 10) */}
            <label>
              Optuna Trials<br />
              <select
                className="dropdown-select"
                value={form.bayesian_trials}
                onChange={(e) => setForm({ ...form, bayesian_trials: e.target.value })}
              >
                {OPTUNA_TRIAL_OPTIONS.map((val) => (
                  <option key={val} value={val}>
                    {val} Trials
                  </option>
                ))}
              </select>
            </label>

            {/* Leverage Dropdown (Multiples of 5) */}
            <label>
              Leverage<br />
              <select
                className="dropdown-select"
                value={form.leverage}
                onChange={(e) => setForm({ ...form, leverage: e.target.value })}
              >
                {LEVERAGE_OPTIONS.map((val) => (
                  <option key={val} value={val}>
                    {val}× Leverage
                  </option>
                ))}
              </select>
            </label>

            <label style={{ display: "flex", alignItems: "center", gap: 6, paddingBottom: 8, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={form.use_bayesian}
                onChange={(e) => setForm({ ...form, use_bayesian: e.target.checked })}
              />
              Optuna Bayesian
            </label>

            <label style={{ display: "flex", alignItems: "center", gap: 6, paddingBottom: 8, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={form.use_atr_sl}
                onChange={(e) => setForm({ ...form, use_atr_sl: e.target.checked })}
              />
              ATR-Adaptive SL
            </label>
          </div>

          {/* Centralized Action Button on Dedicated Row */}
          <div style={{ display: "flex", justifyContent: "center", marginTop: 20 }}>
            <button
              type="submit"
              className="btn primary"
              style={{ minWidth: 220, padding: "11px 32px", fontSize: 14, letterSpacing: "0.03em" }}
            >
              Run Backtest
            </button>
          </div>
        </form>

        {/* Secondary Kaggle Disclosure */}
        <details style={{ marginTop: 16 }}>
          <summary className="hint" style={{ cursor: "pointer", color: "var(--text-muted)" }}>
            ▼ Alternative: Push Code to Kaggle Runner
          </summary>
          <div style={{ marginTop: 8, padding: 12, background: "var(--bg)", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
            <p className="hint" style={{ margin: "0 0 10px 0" }}>
              Pushes configuration constants directly into the Kaggle runner script. Useful as a backup or for inspecting live terminal output.
            </p>
            <button type="button" className="btn" onClick={handleKaggleSync}>
              Sync to Kaggle
            </button>
            {kaggleUrl && (
              <a className="btn" href={kaggleUrl} target="_blank" rel="noopener noreferrer" style={{ marginLeft: 8 }}>
                Open in Kaggle →
              </a>
            )}
          </div>
        </details>

        {statusText && (
          <div className="mono" style={{ marginTop: 12, fontSize: 12, color: "var(--accent-secondary)" }}>
            {statusText}
          </div>
        )}
      </div>

      {/* Auto-Refresh Status Indicator */}
      {isAnyRunActive && (
        <div className="banner info" style={{ marginBottom: 16 }}>
          <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: "var(--amber)", marginRight: 6 }}></span>
          A simulation run is currently in progress. The runs table will auto-update without requiring manual refresh.
        </div>
      )}

      {/* Historical Runs Grid */}
      <div className="grid cols-2">
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <h3 style={{ margin: 0 }}>Walkforward Backtest Runs</h3>
            <button
              type="button"
              className="btn"
              style={{ padding: "3px 8px", fontSize: 11 }}
              onClick={() => loadRuns()}
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
                    <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => showDetail(r.id)}>
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
            <span style={{ fontSize: 11, color: "var(--text-faint)" }}>Once Daily (UTC 00:03)</span>
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
                  <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => showDetail(r.id)}>
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

      {/* Comprehensive Run Modal */}
      {modal && (
        <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && setModal(null)}>
          <div className="modal-card">
            <button className="modal-close" onClick={() => setModal(null)} aria-label="Close">
              &times;
            </button>
            <h3>Run #{modal.runId} Analytics</h3>
            <div className="modal-sub">
              {modal.loading
                ? "Loading simulation telemetry…"
                : modal.error
                ? "Failed to fetch run data"
                : `${modal.run.run_type?.toUpperCase()} · ${modal.run.status?.toUpperCase()} · Completed ${fmtTime(modal.run.finished_at)}`}
            </div>

            {modal.loading && <div className="empty-state">Loading run records…</div>}
            {modal.error && <div className="modal-error">{modal.error}</div>}

            {!modal.loading && !modal.error && modal.run && (
              <>
                {/* 1. CHART DISPLAYED ON TOP */}
                {modal.trades && <EquityCurveChart trades={modal.trades} initialCapital={10.0} />}

                {/* 2. STATS AS IS BELOW THAT */}
                <div className="modal-section" style={{ marginTop: 14 }}>
                  <h4>Performance Metrics Summary</h4>
                  <div className="kv-grid">
                    {Object.entries(modal.run.metrics || {}).map(([k, v]) => {
                      const isTotalTrades = k === "Total Trades" || k === "trade_count";
                      return (
                        <div
                          key={k}
                          className="kv-row"
                          style={isTotalTrades ? { background: "var(--accent-soft)", borderRadius: 4, padding: "5px 8px", cursor: "pointer" } : {}}
                          onClick={() => isTotalTrades && setShowTradesList(!showTradesList)}
                        >
                          <span className="kv-key" style={isTotalTrades ? { color: "var(--accent-secondary)", fontWeight: 600 } : {}}>
                            {k} {isTotalTrades ? (showTradesList ? "▼" : "▶ (Click to inspect)") : ""}
                          </span>
                          <span className="kv-val" style={isTotalTrades ? { color: "#FFF", fontWeight: 700 } : {}}>
                            {String(v ?? "—")}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </div>

                {/* Strategy Parameters */}
                <div className="modal-section">
                  <h4>Parameters Used</h4>
                  <div className="kv-grid">
                    {Object.entries(modal.params || {}).map(([k, v]) => (
                      <div key={k} className="kv-row">
                        <span className="kv-key">{k}</span>
                        <span className="kv-val">{String(v ?? "—")}</span>
                      </div>
                    ))}
                  </div>
                </div>

                {/* 3. CLICKABLE "TOTAL TRADES" EXPANDED LIST */}
                {showTradesList && modal.trades && (
                  <div className="modal-section" style={{ marginTop: 16 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                      <h4 style={{ margin: 0 }}>
                        Executed Trades Log ({modal.trades.length} Trades)
                      </h4>
                      <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
                        Click &quot;Compare to Live&quot; on any trade to inspect model decisions
                      </span>
                    </div>

                    <div style={{ maxHeight: 320, overflowY: "auto", border: "1px solid var(--border-soft)", borderRadius: 6 }}>
                      <table style={{ fontSize: 12 }}>
                        <thead>
                          <tr>
                            <th>Symbol</th>
                            <th>Side</th>
                            <th>Entry & Exit Time</th>
                            <th>Hold Duration</th>
                            <th>Drawdown Risk</th>
                            <th>Return (%)</th>
                            <th style={{ textAlign: "right" }}>Action</th>
                          </tr>
                        </thead>
                        <tbody>
                          {modal.trades.map((tr) => {
                            const isSelected = alignedTrade?.tradeId === tr.id;
                            return (
                              <React.Fragment key={tr.id || tr.entry_ts}>
                                <tr>
                                  <td className="mono" style={{ fontWeight: 600 }}>{tr.symbol}</td>
                                  <td className={tr.side?.toUpperCase().startsWith("B") ? "side-buy" : "side-sell"}>
                                    {tr.side}
                                  </td>
                                  <td className="mono" style={{ fontSize: 11 }}>
                                    {fmtTime(tr.entry_ts)} → {fmtTime(tr.exit_ts)}
                                  </td>
                                  <td className="mono">{formatHoldDuration(tr.entry_ts, tr.exit_ts)}</td>
                                  <td className="mono" style={{ color: "var(--red)" }}>{calculateTradeDrawdown(tr)}</td>
                                  <td className={`mono ${pnlClass(tr.pnl_pct)}`}>{fmtPct(tr.pnl_pct)}</td>
                                  <td style={{ textAlign: "right" }}>
                                    <button
                                      type="button"
                                      className={`btn ${isSelected ? "primary" : ""}`}
                                      style={{ padding: "3px 8px", fontSize: 11 }}
                                      onClick={() => compareTradeToLive(tr)}
                                    >
                                      {isSelected ? "Hide Live" : "Compare to Live"}
                                    </button>
                                  </td>
                                </tr>

                                {/* Interactive Live Model Alignment Drawer */}
                                {isSelected && (
                                  <tr>
                                    <td colSpan={7} style={{ padding: "0 10px 10px 10px", background: "var(--bg)" }}>
                                      <div className="trade-compare-drawer">
                                        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
                                          <span style={{ fontWeight: 600, color: "var(--accent-secondary)" }}>
                                            Live Model Decision Alignment @ {fmtTime(tr.entry_ts)}
                                          </span>
                                          {alignLoading && <span className="mono">Querying historical telemetry…</span>}
                                        </div>

                                        {alignedTrade.error && (
                                          <div style={{ color: "var(--red)" }}>Failed to query live telemetry: {alignedTrade.error}</div>
                                        )}

                                        {alignedTrade.data && (
                                          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
                                            <div>
                                              <div style={{ color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase", marginBottom: 4 }}>
                                                Simulated Backtest Trade
                                              </div>
                                              <div>Entry Price: <span className="mono">{fmtPrice(tr.entry_price)}</span></div>
                                              <div>Exit Price: <span className="mono">{fmtPrice(tr.exit_price)}</span></div>
                                              <div>Exit Leg: <span className="mono">{tr.exit_leg}</span></div>
                                              <div>Realized Return: <span className={`mono ${pnlClass(tr.pnl_pct)}`}>{fmtPct(tr.pnl_pct)}</span></div>
                                            </div>

                                            <div>
                                              <div style={{ color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase", marginBottom: 4 }}>
                                                Live Model Telemetry (Nearest Tick)
                                              </div>
                                              {alignedTrade.data.live_diagnostics ? (
                                                <>
                                                  <div>Trend: <span className="mono">{alignedTrade.data.live_diagnostics.trend?.toUpperCase()}</span></div>
                                                  <div>
                                                    Vol Gate:{" "}
                                                    <span style={{ color: alignedTrade.data.live_diagnostics.vol_gate_passed ? "var(--green)" : "var(--amber)" }}>
                                                      {alignedTrade.data.live_diagnostics.vol_gate_passed ? "PASSED" : "BLOCKED"}
                                                    </span>{" "}
                                                    ({((alignedTrade.data.live_diagnostics.pct_diff || 0) * 100).toFixed(3)}% vs{" "}
                                                    {((alignedTrade.data.live_diagnostics.vol_gate_threshold || 0) * 100).toFixed(3)}%)
                                                  </div>
                                                  <div>
                                                    Strict Gate:{" "}
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
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </Layout>
  );
}