import { useEffect, useRef, useState } from "react";
import Layout from "../components/Layout";
import { apiGet, apiPost, fmtMoney, fmtPct, fmtPrice, fmtTime, pnlClass } from "../lib/api";

// A lightweight inline-SVG line chart of cumulative trade PnL — the
// closest practical analog to walkforward.py's own "Portfolio" equity
// curve panel (run_walk_forward()'s ax1, plotted against capital_curve)
// that's buildable from the per-trade data this UI already has (no
// capital_curve array is persisted per run, only per-trade records —
// see pyquant_backtest_trades/trades_to_records()). Labeled accordingly
// below rather than claimed to be bit-for-bit the same curve.
function EquityCurveChart({ trades }) {
  const sorted = [...trades]
    .filter((t) => t.exit_ts || t.entry_ts)
    .sort((a, b) => new Date(a.exit_ts || a.entry_ts) - new Date(b.exit_ts || b.entry_ts));
  if (sorted.length < 2) return null;

  let running = 0;
  const points = sorted.map((t) => {
    running += t.profit ?? 0;
    return running;
  });
  const min = Math.min(0, ...points);
  const max = Math.max(0, ...points);
  const span = max - min || 1;
  const W = 760, H = 180, PAD = 8;
  const toX = (i) => PAD + (i / (points.length - 1)) * (W - PAD * 2);
  const toY = (v) => PAD + (1 - (v - min) / span) * (H - PAD * 2);
  const path = points.map((v, i) => `${i === 0 ? "M" : "L"} ${toX(i).toFixed(1)} ${toY(v).toFixed(1)}`).join(" ");
  const zeroY = toY(0).toFixed(1);
  const final = points[points.length - 1];
  const stroke = final >= 0 ? "var(--green)" : "var(--red)";

  return (
    <div className="modal-section">
      <h4>Equity Curve (cumulative trade PnL)</h4>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ display: "block" }}>
        <line x1={PAD} y1={zeroY} x2={W - PAD} y2={zeroY} stroke="var(--border)" strokeWidth="1" strokeDasharray="4 3" />
        <path d={path} fill="none" stroke={stroke} strokeWidth="1.6" />
      </svg>
      <div className="hint" style={{ marginTop: 4 }}>
        Cumulative $ PnL across this run&apos;s {sorted.length} trades, in exit order — the nearest equivalent
        to walkforward.py&apos;s own equity-curve plot buildable from the per-trade data persisted for this run
        (the exact capital_curve array it plots isn&apos;t itself persisted).
      </div>
    </div>
  );
}

function KvGrid({ obj }) {
  const keys = Object.keys(obj || {});
  if (keys.length === 0) return <div className="empty-state" style={{ padding: "8px 0" }}>None</div>;
  return (
    <div className="kv-grid">
      {keys.map((k) => (
        <div className="kv-row" key={k}>
          <span className="kv-key">{k}</span>
          <span className="kv-val">{obj[k] === null || obj[k] === undefined ? "—" : String(obj[k])}</span>
        </div>
      ))}
    </div>
  );
}

export default function Backtest() {
  const [wfRuns, setWfRuns] = useState(null);
  const [optRuns, setOptRuns] = useState(null);
  const [loadError, setLoadError] = useState(null);

  const [form, setForm] = useState({
    walk_forward_days: "", start_date: "", end_date: "", bayesian_trials: "", leverage: "",
    use_bayesian: true, use_atr_sl: true,
  });
  const [statusText, setStatusText] = useState("");
  const [kaggleUrl, setKaggleUrl] = useState(null);
  const pollRef = useRef(null);

  const [modal, setModal] = useState(null); // { runId, loading, run, params, error, trades }

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
    return () => clearInterval(pollRef.current);
  }, []);

  function formBody() {
    const body = {};
    // start_date/end_date (when start_date is set) take priority over
    // walk_forward_days — see walkforward.py's START_DATE/END_DATE and
    // BacktestRequest's docstring in api/index.py.
    if (form.start_date) {
      body.start_date = form.start_date;
      if (form.end_date) body.end_date = form.end_date;
    } else if (form.walk_forward_days) {
      body.walk_forward_days = parseInt(form.walk_forward_days, 10);
    }
    if (form.bayesian_trials) body.bayesian_trials = parseInt(form.bayesian_trials, 10);
    if (form.leverage) body.leverage = parseFloat(form.leverage);
    body.use_bayesian = !!form.use_bayesian;
    body.use_atr_sl = !!form.use_atr_sl;
    return body;
  }

  function pollRun(runId) {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const run = await apiGet(`/runs/${runId}`);
        if (run.status === "completed") {
          clearInterval(pollRef.current);
          setStatusText(`Run #${runId} completed.`);
          loadRuns();
        } else if (run.status === "failed") {
          clearInterval(pollRef.current);
          setStatusText(`Run #${runId} failed: ${run.error || "unknown error"}`);
        } else {
          setStatusText(`Run #${runId}: ${run.status}…`);
        }
      } catch (e) {
        setStatusText(`Lost track of run #${runId} (${e.message}) — check the Kaggle dashboard directly.`);
        clearInterval(pollRef.current);
      }
    }, 15000); // Kaggle's own queue is usually minutes, not seconds — no need to poll faster
  }

  // 2026-10-29: GitHub Actions is now the primary, default path — it's
  // been confirmed working live (the sys.path ModuleNotFoundError that
  // blocked it is fixed), so "Run Backtest" triggers it directly with
  // no manual step. Kaggle (the original design, still fully wired) is
  // kept as a backup behind the "Backup: run on Kaggle instead"
  // disclosure.
  async function handleSubmit(e) {
    e.preventDefault();
    setStatusText("Submitting to GitHub Actions…");
    try {
      const result = await apiPost("/backtests", formBody());
      setStatusText(`Run #${result.run_id} queued — GitHub Actions is starting it now.`);
      pollRun(result.run_id);
    } catch (err) {
      setStatusText("Could not start the run: " + err.message);
    }
  }

  async function handleKaggleSync() {
    setKaggleUrl(null);
    setStatusText("Pushing to Kaggle…");
    try {
      const result = await apiPost("/backtests/sync-to-kaggle", formBody());
      setStatusText(`Run #${result.run_id} pushed and queued.`);
      setKaggleUrl(result.kaggle_kernel_url);
      pollRun(result.run_id);
    } catch (err) {
      setStatusText("Could not push to Kaggle: " + err.message);
    }
  }

  async function showDetail(runId) {
    setModal({ runId, loading: true, run: null, params: {}, error: null, trades: null });
    try {
      const run = await apiGet(`/runs/${runId}`);
      let params = {};
      try {
        params = typeof run.params === "string" ? JSON.parse(run.params) : (run.params || {});
      } catch { /* leave as {} */ }

      let trades = null;
      // 2026-10-28: per-trade data (foundation for the Backtest-vs-Live
      // comparison, now surfaced further on the Compare page too).
      // Loaded separately/lazily since it's a second request and most
      // runs predate this feature (empty list, nothing to show).
      if (run.run_type === "walkforward") {
        try {
          const t = await apiGet(`/runs/${runId}/trades`);
          if (t.trades && t.trades.length > 0) trades = t.trades;
        } catch { /* older runs simply have no persisted trades yet */ }
      }
      setModal({ runId, loading: false, run, params, error: null, trades });
    } catch (e) {
      setModal({ runId, loading: false, run: null, params: {}, error: e.message, trades: null });
    }
  }

  function closeDetail() {
    setModal(null);
  }

  useEffect(() => {
    function onKey(e) { if (e.key === "Escape") closeDetail(); }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <Layout title="Backtest" footer="Click a run to see its full metrics/params payload.">
      {loadError && <div className="banner error">Could not reach the API ({loadError}).</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Run a new backtest</h3>
        <p className="hint" style={{ marginTop: -4 }}>
          &quot;Run Backtest&quot; starts the same walkforward logic as an automated GitHub Actions job — no
          manual step, result syncs back here automatically. Set a start date to test an explicit window instead
          of &quot;last N days&quot; (end date defaults to the latest available data if left blank) — this
          disables Days, which only applies when no start date is set. Dates must fall within the ~366-day
          window the historical data covers.
        </p>
        <form onSubmit={handleSubmit} style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
          <label>Days<br />
            <input type="number" min="1" placeholder="15" style={{ width: 80 }}
              disabled={!!form.start_date}
              value={form.walk_forward_days} onChange={(e) => setForm({ ...form, walk_forward_days: e.target.value })} />
          </label>
          <label>Start date<br />
            <input type="date" style={{ width: 150 }}
              value={form.start_date} onChange={(e) => setForm({ ...form, start_date: e.target.value })} />
          </label>
          <label>End date (optional)<br />
            <input type="date" style={{ width: 150 }}
              disabled={!form.start_date}
              value={form.end_date} onChange={(e) => setForm({ ...form, end_date: e.target.value })} />
          </label>
          <label>Bayesian trials<br />
            <input type="number" min="1" placeholder="150" style={{ width: 90 }}
              value={form.bayesian_trials} onChange={(e) => setForm({ ...form, bayesian_trials: e.target.value })} />
          </label>
          <label>Leverage<br />
            <input type="number" min="1" step="0.5" placeholder="10" style={{ width: 80 }}
              value={form.leverage} onChange={(e) => setForm({ ...form, leverage: e.target.value })} />
          </label>
          <label>
            <input type="checkbox" checked={form.use_bayesian}
              onChange={(e) => setForm({ ...form, use_bayesian: e.target.checked })} /> Bayesian (Optuna)
          </label>
          <label>
            <input type="checkbox" checked={form.use_atr_sl}
              onChange={(e) => setForm({ ...form, use_atr_sl: e.target.checked })} /> ATR-adaptive SL
          </label>
          <button type="submit" className="btn primary">Run Backtest</button>
        </form>
        <details style={{ marginTop: 10 }}>
          <summary className="hint" style={{ cursor: "pointer" }}>▼ Backup: run on Kaggle instead</summary>
          <p className="hint">
            Pushes these same values into the Kaggle kernel&apos;s own editor and leaves the run queued —
            click &quot;Run on Kaggle&quot; (appears once the push succeeds) to open it and hit &quot;Save &amp; Run
            All&quot; yourself for full, visible console output, which then syncs its result back to this page
            automatically. Use this if GitHub Actions is unavailable or you want to watch the run live. See
            KAGGLE.md Part K.
          </p>
          <button type="button" className="btn" onClick={handleKaggleSync}>Sync to Kaggle</button>
          {kaggleUrl && (
            <a className="btn" href={kaggleUrl} target="_blank" rel="noopener noreferrer"
               style={{ textDecoration: "none", marginLeft: 8 }}>
              Run on Kaggle →
            </a>
          )}
        </details>
        <div className="mono" style={{ marginTop: 8, fontSize: 12, color: "var(--text-dim)" }}>{statusText}</div>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h3>Walkforward Runs</h3>
          <table>
            <thead><tr><th>Run</th><th>Return</th><th>Win Rate</th><th>Trades</th><th>Finished</th></tr></thead>
            <tbody>
              {wfRuns && wfRuns.map((r) => {
                const m = r.metrics || {};
                return (
                  <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => showDetail(r.id)}>
                    <td className="mono">#{r.id}</td>
                    <td className="mono">{m["Total Return (%)"] ?? "—"}%</td>
                    <td className="mono">{m["Win Rate (%)"] ?? "—"}%</td>
                    <td className="mono">{m["Total Trades"] ?? "—"}</td>
                    <td>{fmtTime(r.finished_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {wfRuns && wfRuns.length === 0 && (
            <div className="empty-state">
              No walkforward runs logged yet — run <code className="mono">python3 walkforward.py</code> to log one.
            </div>
          )}
        </div>
        <div className="card">
          <h3>Daily Optimizer Runs</h3>
          <table>
            <thead><tr><th>Run</th><th>BTC bal</th><th>ETH bal</th><th>Finished</th></tr></thead>
            <tbody>
              {optRuns && optRuns.map((r) => {
                const m = r.metrics || {};
                return (
                  <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => showDetail(r.id)}>
                    <td className="mono">#{r.id}</td>
                    <td className="mono">{fmtMoney(m.btc_val)}</td>
                    <td className="mono">{fmtMoney(m.eth_val)}</td>
                    <td>{fmtTime(r.finished_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {optRuns && optRuns.length === 0 && (
            <div className="empty-state">
              No optimizer runs logged yet — <code className="mono">daily_optimizer()</code> is triggered once
              a day by the GitHub Actions cron tick (whichever ~5-minute tick first reaches a new UTC day; not
              a fixed 00:03 anymore under the current hosting — see SPEC.md Section 3), and only a
              <em> completed</em> run is recorded here. If the Live Monitor page&apos;s Engine Heartbeat shows
              the cron job ticking but this stays empty past a full day, check the GitHub Actions run history
              for errors inside <code className="mono">daily_optimizer()</code> itself.
            </div>
          )}
        </div>
      </div>

      {modal && (
        <div className="modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) closeDetail(); }}>
          <div className="modal-card">
            <button className="modal-close" onClick={closeDetail} aria-label="Close">&times;</button>
            <h3>Run #{modal.runId}</h3>
            <div className="modal-sub">
              {modal.loading
                ? "Loading…"
                : modal.error
                  ? "Error"
                  : `${modal.run.run_type || "—"} · ${modal.run.status || "—"} · finished ${fmtTime(modal.run.finished_at)}`}
            </div>
            {modal.loading && <div>Loading…</div>}
            {modal.error && <div className="modal-error">Could not load run: {modal.error}</div>}
            {!modal.loading && !modal.error && modal.run && (
              <>
                <div className="modal-section">
                  <h4>Metrics</h4>
                  <KvGrid obj={modal.run.metrics || {}} />
                </div>
                <div className="modal-section">
                  <h4>Parameters used</h4>
                  <KvGrid obj={modal.params} />
                </div>
                {modal.run.error && (
                  <div className="modal-section">
                    <h4>Error</h4>
                    <div className="modal-error">{modal.run.error}</div>
                  </div>
                )}
                {modal.run.kaggle_ref && (
                  <div className="modal-section">
                    <h4>Kaggle reference</h4>
                    <div className="mono" style={{ fontSize: 13 }}>{modal.run.kaggle_ref}</div>
                  </div>
                )}
                {modal.trades && <EquityCurveChart trades={modal.trades} />}
                {modal.trades && (
                  <div className="modal-section">
                    <h4>Per-trade data</h4>
                    <table style={{ fontSize: 12 }}>
                      <thead>
                        <tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th><th>Exit via</th><th>PnL %</th><th>Opened</th></tr>
                      </thead>
                      <tbody>
                        {modal.trades.map((tr, i) => (
                          <tr key={i}>
                            <td className="mono">{tr.symbol ?? "—"}</td>
                            <td className={tr.side === "Buy" ? "side-buy" : "side-sell"}>{tr.side ?? "—"}</td>
                            <td className="mono">{fmtPrice(tr.entry_price)}</td>
                            <td className="mono">{fmtPrice(tr.exit_price)}</td>
                            <td>{tr.exit_leg ?? "—"}</td>
                            <td className={`mono ${pnlClass(tr.pnl_pct)}`}>{fmtPct(tr.pnl_pct)}</td>
                            <td>{fmtTime(tr.entry_ts)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <div className="hint" style={{ marginTop: 6 }}>
                      {modal.trades.length} trade(s) — see the Compare page&apos;s Day-by-Day section for how
                      this run&apos;s trades stack up against live performance.
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
