import React, { useEffect, useState } from "react";
import Layout from "../components/Layout";
import { apiGet, fmtPrice, fmtTime, diagStageLabel, pyqInsight, pyqInsightTone } from "../lib/api";

// 2026-11-02: restores the diagnostics-HISTORY chart (GET
// /api/diagnostics/history) that this page's rewrite dropped while the
// footer text below still claimed to stream from it. Solid line = how
// far price has actually moved from the UKF prediction each tick;
// dashed line = the volatility gate's required move; green dots mark
// ticks where a signal actually fired.
function DiagHistoryChart({ rows }) {
  const sorted = [...rows].reverse();
  if (sorted.length < 2) return null;
  const vals = sorted.map((r) => r.pct_diff).filter((v) => v !== null && v !== undefined);
  const thresholds = sorted.map((r) => r.vol_gate_threshold).filter((v) => v !== null && v !== undefined);
  if (vals.length < 2) return null;
  const max = Math.max(...vals, ...thresholds, 0.0001) * 1.1;
  const W = 760, H = 110, PAD = 6;
  const n = sorted.length;
  const toX = (i) => PAD + (i / (n - 1)) * (W - PAD * 2);
  const toY = (v) => PAD + (1 - (v ?? 0) / max) * (H - PAD * 2);
  let path = "", started = false;
  sorted.forEach((r, i) => {
    if (r.pct_diff === null || r.pct_diff === undefined) { started = false; return; }
    path += `${started ? "L" : "M"} ${toX(i).toFixed(1)} ${toY(r.pct_diff).toFixed(1)} `;
    started = true;
  });
  const avgThreshold = thresholds.length ? thresholds.reduce((a, b) => a + b, 0) / thresholds.length : null;
  const firedXs = sorted.map((r, i) => (r.signal_fired ? toX(i) : null)).filter((x) => x !== null);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} style={{ display: "block" }}>
      {avgThreshold !== null && (
        <line x1={PAD} y1={toY(avgThreshold)} x2={W - PAD} y2={toY(avgThreshold)}
          stroke="var(--amber)" strokeWidth="1" strokeDasharray="4 3" />
      )}
      <path d={path} fill="none" stroke="var(--accent-secondary)" strokeWidth="1.4" />
      {firedXs.map((x, i) => (
        <circle key={i} cx={x} cy={PAD} r="3" fill="var(--green)" />
      ))}
    </svg>
  );
}

// 2026-11-03 ("PyQ Insights"): the chart above shows raw numbers with no
// explanation — this translates the latest tick's gate state into plain
// language (via lib/api.js's pyqInsight()) plus a quick tally of how the
// recent window broke down (fired / waiting for retrace / blocked at the
// volatility gate / flat), so the history can be understood at a glance
// instead of requiring a hover-by-hover read of the chart.
function PyqInsightsPanel({ rows }) {
  if (!rows || rows.length === 0) return null;
  const latest = rows[0]; // diagHistory is newest-first (list_diagnostic_history ORDER BY id DESC)
  const counts = {};
  rows.forEach((r) => {
    const tone = pyqInsightTone(r);
    counts[tone] = (counts[tone] || 0) + 1;
  });
  const toneMeta = {
    fired: { color: "var(--green)", label: "Signal fired" },
    waiting_retrace: { color: "var(--amber)", label: "Waiting for retrace" },
    // matches ClearanceGauge's "NO RETRACE" case below — strict_diff is
    // already past threshold but the predicted band is on the wrong side
    // of pred for this trend, so more retrace won't fix it.
    blocked_band_invalid: { color: "var(--amber)", label: "Band inverted (not a retrace wait)" },
    blocked_vol: { color: "var(--text-muted)", label: "Volatility gate blocked" },
    flat: { color: "var(--text-faint)", label: "Flat / no edge" },
    unknown: { color: "var(--text-faint)", label: "Unclear" },
  };

  return (
    <div style={{ marginTop: 8, padding: "10px 12px", background: "var(--bg)", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: "var(--accent-secondary)", textTransform: "uppercase", letterSpacing: "0.04em" }}>
          PyQ Insights — latest tick
        </span>
        <span style={{ fontSize: 10, color: "var(--text-faint)" }}>{fmtTime(latest.ts)}</span>
      </div>
      <div style={{ fontSize: 12, color: "var(--text-dim)", lineHeight: 1.5 }}>{pyqInsight(latest)}</div>
      <div style={{ display: "flex", gap: 12, marginTop: 8, flexWrap: "wrap" }}>
        {Object.entries(counts).map(([tone, n]) => (
          <span key={tone} style={{ fontSize: 10, color: (toneMeta[tone] || toneMeta.unknown).color }}>
            ● {(toneMeta[tone] || toneMeta.unknown).label}: {n}/{rows.length}
          </span>
        ))}
      </div>
    </div>
  );
}

function ClearanceGauge({ passed, ratio, label, details }) {
  const boundedPct = Math.min(100, Math.max(0, ratio * 100));
  
  // If the directional check failed despite high ratio, show clear status
  let statusText = `${boundedPct.toFixed(0)}%`;
  let barColor = "var(--border)";

  if (passed) {
    statusText = "CLEARED";
    barColor = "var(--green)";
  } else if (boundedPct >= 100) {
    statusText = "NO RETRACE";
    barColor = "var(--amber)";
  } else if (boundedPct > 70) {
    statusText = `${boundedPct.toFixed(0)}%`;
    barColor = "var(--amber)";
  }

  return (
    <div style={{ minWidth: 145 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 3 }}>
        <span style={{ color: passed ? "var(--green)" : boundedPct >= 100 ? "var(--amber)" : "var(--text-muted)", fontWeight: 600 }}>
          {statusText}
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
  const [error, setError] = useState(null);
  const [diagHistory, setDiagHistory] = useState(null);
  const [shadowHistory, setShadowHistory] = useState(null);
  const [sigSource, setSigSource] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/heartbeat")
      .then((h) => !cancelled && setHeartbeat(h))
      .catch((e) => !cancelled && setError(e.message));
    apiGet("/signals/open")
      .then((d) => !cancelled && setLive(d))
      .catch((e) => !cancelled && setError(e.message));
    apiGet("/signal-source")
      .then((d) => {
        if (cancelled) return;
        setSigSource(d);
        const liveSrc = d.live_signal_source || "p4_search";
        // 2026-11-03 (shadow signal comparison): fetch each source
        // SEPARATELY rather than one unfiltered call — two optimizers'
        // readings interleaved in one array would zig-zag the chart
        // below as if it were one continuous series. The live source
        // drives the main chart/PyQ Insights; the other (if shadow
        // comparison is on) gets its own, clearly-labeled panel.
        apiGet("/diagnostics/history", { limit: 300, source: liveSrc })
          .then((hd) => !cancelled && setDiagHistory(hd.history))
          .catch(() => {});
        if (d.shadow_comparison_enabled) {
          const shadowSrc = liveSrc === "p4_search" ? "walkforward" : "p4_search";
          apiGet("/diagnostics/history", { limit: 300, source: shadowSrc })
            .then((hd) => !cancelled && setShadowHistory(hd.history))
            .catch(() => {});
        }
      })
      .catch(() => {
        // signal-source unreachable — fall back to the old unfiltered
        // fetch rather than showing nothing (matches pre-2026-11-03
        // behavior when shadow comparison doesn't exist/isn't reachable).
        if (!cancelled) {
          apiGet("/diagnostics/history", { limit: 300 })
            .then((hd) => !cancelled && setDiagHistory(hd.history))
            .catch(() => {});
        }
      });
    return () => { cancelled = true; };
  }, []);

  const diag = heartbeat?.signal_diagnostics || {};
  const symbols = Object.keys(diag);

  return (
    <Layout title="Live Monitor" footer="Real-time gate telemetry streams from pyquant_diagnostics_history and pyquant_state.">
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
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Signal Generation Gates (Last Hourly Tick)</h3>
          <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
            Evaluated every hour at :02 past the hour
          </span>
        </div>

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
          <div className="empty-state">No gate diagnostics logged yet. Next tick evaluates at :02 past the hour.</div>
        )}
      </div>

      {/* Signal Diagnostics History — one row per tick, not just the
          latest, so a quiet stretch (strategy correctly waiting) reads
          as a trend rather than a single opaque snapshot. */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <h3 style={{ margin: 0 }}>Signal Diagnostics History</h3>
          {sigSource && (
            <span className="mono" style={{ fontSize: 10, color: "var(--text-faint)" }}>
              Live source: <strong style={{ color: "var(--accent-secondary)" }}>
                {(sigSource.live_signal_source || "p4_search").toUpperCase()}
              </strong>
              {sigSource.shadow_comparison_enabled ? " · shadow comparison ON" : ""}
            </span>
          )}
        </div>
        {diagHistory && diagHistory.length === 0 && (
          <div className="empty-state">No diagnostics history yet — it accumulates from this point forward.</div>
        )}
        {diagHistory && diagHistory.length > 0 && (
          [...new Set(diagHistory.map((r) => r.symbol))].map((sym) => {
            const rows = diagHistory.filter((r) => r.symbol === sym);
            const shadowRows = (shadowHistory || []).filter((r) => r.symbol === sym);
            return (
              <div key={sym} style={{ marginTop: 10 }}>
                <div className="stat-label" style={{ marginBottom: 4 }}>{sym} — last {rows.length} tick(s)</div>
                <DiagHistoryChart rows={rows} />
                <PyqInsightsPanel rows={rows} />
                {shadowRows.length > 0 && (
                  <div style={{ marginTop: 8 }}>
                    <div className="stat-label" style={{ marginBottom: 4, color: "var(--text-faint)" }}>
                      Shadow ({(sigSource.live_signal_source === "p4_search" ? "walkforward" : "p4_search").toUpperCase()}) —
                      recorded only, never drives a real order
                    </div>
                    <PyqInsightsPanel rows={shadowRows} />
                  </div>
                )}
              </div>
            );
          })
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
                <td className="mono">{fmtPrice(s.entry_capital)}</td>
                <td className="mono">{fmtPrice(s.expected_max_loss)}</td>
                <td><span className="pill open">{s.mode}</span></td>
                <td>{fmtTime(s.opened_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {(!live?.signals || live.signals.length === 0) && (
          <div className="empty-state">Zero open positions. The strategy is waiting for gate clearance.</div>
        )}
      </div>
    </Layout>
  );
}