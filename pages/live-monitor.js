import { useEffect, useState } from "react";
import Layout from "../components/Layout";
import { apiGet, fmtPrice, fmtTime, fmtMoney, diagStageLabel } from "../lib/api";

function ClearanceGauge({ passed, ratio, label, details }) {
  const boundedPct = Math.min(100, Math.max(0, ratio * 100));
  const barColor = passed ? "var(--green)" : boundedPct > 75 ? "var(--amber)" : "var(--border)";

  return (
    <div style={{ minWidth: 140 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 3 }}>
        <span style={{ color: passed ? "var(--green)" : "var(--text-muted)", fontWeight: 600 }}>
          {passed ? "CLEARED" : `${boundedPct.toFixed(0)}%`}
        </span>
        <span style={{ color: "var(--text-faint)", fontSize: 10 }}>{label}</span>
      </div>
      <div style={{ width: "100%", height: 6, background: "var(--bg)", borderRadius: 3, overflow: "hidden" }}>
        <div style={{ width: `${boundedPct}%`, height: "100%", background: barColor, transition: "width 0.3s ease" }} />
      </div>
      <div style={{ fontSize: 10, color: "var(--text-faint)", marginTop: 2 }} className="mono">
        {details}
      </div>
    </div>
  );
}

export default function LiveMonitor() {
  const [heartbeat, setHeartbeat] = useState(null);
  const [live, setLive] = useState(null);
  const [diagHistory, setDiagHistory] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/heartbeat")
      .then((h) => !cancelled && setHeartbeat(h))
      .catch((e) => !cancelled && setError(e.message));
    apiGet("/signals/open")
      .then((d) => !cancelled && setLive(d))
      .catch((e) => !cancelled && setError(e.message));
    apiGet("/diagnostics/history", { limit: 200 })
      .then((d) => !cancelled && setDiagHistory(d.history))
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, []);

  const diag = heartbeat?.signal_diagnostics || {};
  const symbols = Object.keys(diag);

  return (
    <Layout title="Live Monitor" footer="Telemetry streams from pyquant_state and pyquant_diagnostics_history.">
      {error && <div className="banner error">Connection Notice: {error}</div>}

      {/* Heartbeat Status Cards */}
      <div className="grid cols-2" style={{ marginBottom: 16 }}>
        <div className="card">
          <h3>Engine Heartbeat (5M Cron)</h3>
          {heartbeat ? (
            <div>
              <div style={{ fontSize: 16, fontWeight: 600, color: heartbeat.cron_age_seconds < 360 ? "var(--green)" : "var(--amber)" }}>
                ● {heartbeat.cron_age_seconds < 360 ? "ONLINE & TICKING" : "RUNNER DELAYED"}
              </div>
              <div className="stat-label">
                Last Tick: {heartbeat.cron_age_seconds ? `${Math.floor(heartbeat.cron_age_seconds / 60)} min ago` : "Pending first run"}
              </div>
            </div>
          ) : (
            <div className="stat-label">Checking runner status…</div>
          )}
        </div>

        <div className="card">
          <h3>1H UKF Dynamic Re-Anchor</h3>
          {heartbeat?.ukf_retrain ? (
            <div>
              <div style={{ fontSize: 16, fontWeight: 600, color: heartbeat.ukf_retrain.status === "ok" ? "var(--green)" : "var(--red)" }}>
                ● {heartbeat.ukf_retrain.status === "ok" ? "ANCHORED & STABLE" : "RE-ANCHOR FAILED"}
              </div>
              <div className="stat-label">
                Last Refit: {heartbeat.ukf_retrain_age_seconds ? `${(heartbeat.ukf_retrain_age_seconds / 3600).toFixed(1)} hrs ago` : "Pending"}
              </div>
            </div>
          ) : (
            <div className="stat-label">Checking UKF state…</div>
          )}
        </div>
      </div>

      {/* Signal Diagnostics Clearance Table */}
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Signal Generation Gates (Last Hourly Tick)</h3>
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Trend</th>
              <th>Market Price</th>
              <th>UKF Pred</th>
              <th>Volatility Gate</th>
              <th>Strict Entry Gate</th>
              <th>Outcome</th>
              <th>Evaluated At</th>
            </tr>
          </thead>
          <tbody>
            {symbols.map((sym) => {
              const dg = diag[sym];
              const volRatio = dg.vol_gate_threshold ? (dg.pct_diff || 0) / dg.vol_gate_threshold : 0;
              const strictRatio = dg.strict_threshold ? (dg.strict_diff || 0) / dg.strict_threshold : 0;

              return (
                <tr key={sym}>
                  <td className="mono" style={{ fontWeight: 600 }}>{sym}</td>
                  <td>
                    <span className={`pill ${dg.trend === "up" ? "closed_tp" : dg.trend === "down" ? "closed_sl" : "open"}`}>
                      {dg.trend?.toUpperCase() || "FLAT"}
                    </span>
                  </td>
                  <td className="mono">{fmtPrice(dg.price)}</td>
                  <td className="mono">{fmtPrice(dg.pred)}</td>
                  <td>
                    <ClearanceGauge
                      passed={dg.vol_gate_passed}
                      ratio={volRatio}
                      label="Vol Gate"
                      details={`${((dg.pct_diff || 0) * 100).toFixed(3)}% / ${((dg.vol_gate_threshold || 0) * 100).toFixed(3)}%`}
                    />
                  </td>
                  <td>
                    <ClearanceGauge
                      passed={dg.strict_gate_passed}
                      ratio={strictRatio}
                      label="Entry Gate"
                      details={`${((dg.strict_diff || 0) * 100).toFixed(3)}% / ${((dg.strict_threshold || 0) * 100).toFixed(3)}%`}
                    />
                  </td>
                  <td>
                    <span className={`pill ${dg.signal_fired ? "closed_tp" : "open"}`}>
                      {dg.signal_fired ? "SIGNAL FIRED" : diagStageLabel(dg)}
                    </span>
                  </td>
                  <td>{fmtTime(dg.ts)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {symbols.length === 0 && (
          <div className="empty-state">No gate diagnostics logged yet. Hourly tasks record state at :02 past the hour.</div>
        )}
      </div>

      {/* Active Position Tracking */}
      <div className="card">
        <h3>Currently Monitored Positions</h3>
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Entry</th>
              <th>SL</th>
              <th>TP</th>
              <th>Notional</th>
              <th>Max Loss</th>
              <th>Mode</th>
              <th>Opened</th>
            </tr>
          </thead>
          <tbody>
            {live?.signals?.map((s, i) => (
              <tr key={i}>
                <td className="mono">{s.symbol}</td>
                <td className={s.side === "BUY" ? "side-buy" : "side-sell"}>{s.side}</td>
                <td className="mono">{fmtPrice(s.entry_price)}</td>
                <td className="mono">{fmtPrice(s.sl_price)}</td>
                <td className="mono">{fmtPrice(s.tp_price)}</td>
                <td className="mono">{fmtMoney(s.entry_capital)}</td>
                <td className="mono">{fmtMoney(s.expected_max_loss)}</td>
                <td><span className="pill open">{s.mode}</span></td>
                <td>{fmtTime(s.opened_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {(!live?.signals || live.signals.length === 0) && (
          <div className="empty-state">Zero positions currently active. Strategy is awaiting trigger threshold.</div>
        )}
      </div>
    </Layout>
  );
}