import { useEffect, useState } from "react";
import Layout from "../components/Layout";
import { apiGet, fmtPrice, fmtTime, fmtMoney, diagStageLabel } from "../lib/api";

// Small inline-SVG line chart — pct_diff (how far price has actually
// moved from the UKF prediction) against its vol_gate_threshold (how
// far it needs to move to clear the gate) over the recent diagnostics
// history for one symbol. A line that stays well under the dashed
// threshold for a long stretch is the strategy correctly waiting, not
// a stuck engine; a line that crosses it with signal_fired still false
// nearby points at something worth checking in the entry ("strict")
// gate instead. Rows arrive newest-first from the API; charted oldest-
// first (left to right).
function DiagHistoryChart({ rows }) {
  const sorted = [...rows].reverse();
  if (sorted.length < 2) return null;
  const vals = sorted.map((r) => r.pct_diff).filter((v) => v !== null && v !== undefined);
  const thresholds = sorted.map((r) => r.vol_gate_threshold).filter((v) => v !== null && v !== undefined);
  if (vals.length < 2) return null;
  const max = Math.max(...vals, ...thresholds, 0.0001) * 1.1;
  const W = 760, H = 120, PAD = 6;
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
      <path d={path} fill="none" stroke="var(--accent)" strokeWidth="1.4" />
      {firedXs.map((x, i) => (
        <circle key={i} cx={x} cy={PAD} r="3" fill="var(--green)" />
      ))}
    </svg>
  );
}

// live-monitor.html's inline script locally redefined fmtPct with
// different precision/no sign (3 decimals, raw) from the shared
// app.js/lib/api.js version (2 decimals, signed) — kept as its own
// function here too, scoped to this page only, exactly like the
// original.
function fmtDiagPct(v) {
  if (v === null || v === undefined) return "—";
  return (Number(v) * 100).toFixed(3) + "%";
}

export default function LiveMonitor() {
  const [heartbeat, setHeartbeat] = useState(null);
  const [hbError, setHbError] = useState(null);
  const [live, setLive] = useState(null);
  const [liveError, setLiveError] = useState(null);
  const [diagHistory, setDiagHistory] = useState(null);
  const [diagHistoryError, setDiagHistoryError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/heartbeat")
      .then((h) => { if (!cancelled) setHeartbeat(h); })
      .catch((e) => { if (!cancelled) setHbError(e.message); });
    apiGet("/signals/open")
      .then((d) => { if (!cancelled) setLive(d); })
      .catch((e) => { if (!cancelled) setLiveError(e.message); });
    apiGet("/diagnostics/history", { limit: 300 })
      .then((d) => { if (!cancelled) setDiagHistory(d.history); })
      .catch((e) => { if (!cancelled) setDiagHistoryError(e.message); });
    return () => { cancelled = true; };
  }, []);

  let hbLine = { color: null, text: "Checking…" };
  if (hbError) {
    hbLine = { color: "#e5484d", text: `Could not reach the API (${hbError}).` };
  } else if (heartbeat) {
    const age = heartbeat.cron_age_seconds;
    if (age === null || age === undefined) {
      hbLine = {
        color: "var(--text-faint)",
        text: "No cron tick recorded yet — either the engine hasn't run since this was added, or it isn't running. Check the GitHub Actions run history.",
      };
    } else {
      const mins = Math.floor(age / 60);
      if (age < 360) hbLine = { color: "#3ddc84", text: `● Live — last tick ${mins < 1 ? "under a minute" : mins + " min"} ago` };
      else if (age < 900) hbLine = { color: "#f5a623", text: `● Stale — last tick ${mins} min ago (expected every ~5 min)` };
      else hbLine = { color: "#e5484d", text: `● Not reporting — last tick ${mins} min ago. Check the GitHub Actions run history.` };
    }
  }

  // 2026-10-27: separate from the 5-min cron heartbeat above — specifically
  // "did the once-daily 1H UKF re-anchor actually happen today", added
  // after a live incident where a slow parameter search timed out the job
  // before the re-anchor ran, silently stalling signal generation for days
  // with nothing in the UI pointing at why.
  let ukfLine = { color: null, text: "Checking…" };
  if (heartbeat) {
    const ukf = heartbeat.ukf_retrain;
    const ukfAge = heartbeat.ukf_retrain_age_seconds;
    if (!ukf || ukfAge === null || ukfAge === undefined) {
      ukfLine = {
        color: "var(--text-faint)",
        text: "UKF re-anchor: no record yet — either this hasn't shipped to this deployment yet, or it hasn't ticked since.",
      };
    } else {
      const hrs = (ukfAge / 3600).toFixed(1);
      if (ukf.status === "failed") {
        ukfLine = { color: "#e5484d", text: `● UKF re-anchor FAILED ${hrs}h ago: ${ukf.error || "unknown error"}` };
      } else if (ukfAge > 36 * 3600) {
        ukfLine = { color: "#e5484d", text: `● UKF re-anchor overdue — last success ${hrs}h ago (expected daily). Check GitHub Actions run history.` };
      } else {
        ukfLine = { color: "#3ddc84", text: `● UKF re-anchored ${hrs}h ago` };
      }
    }
  }

  const diag = (heartbeat && heartbeat.signal_diagnostics) || {};
  const symbols = Object.keys(diag);

  return (
    <Layout title="Live Monitor" footer="Reflects pyquant_state's signals log as of the last entry/close the trading engine wrote. Refresh the page for the latest.">
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Engine Heartbeat</h3>
        <div className="stat-label" style={hbLine.color ? { color: hbLine.color } : undefined}>{hbLine.text}</div>
        <div className="stat-label" style={{ marginTop: 8, ...(ukfLine.color ? { color: ukfLine.color } : {}) }}>{ukfLine.text}</div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Signal Diagnostics (last hourly tick, each symbol)</h3>
        <table>
          <thead>
            <tr>
              <th>Symbol</th><th>Trend</th><th>Price</th><th>Predicted</th>
              <th>% Move</th><th>Vol Gate</th><th>Entry Gate</th><th>Fired?</th><th>As of</th>
            </tr>
          </thead>
          <tbody>
            {symbols.map((sym) => {
              const dg = diag[sym];
              const volGate = dg.vol_gate_passed === undefined
                ? "—"
                : (
                  <>
                    <span style={{ color: dg.vol_gate_passed ? "#3ddc84" : "#f5a623" }}>
                      {dg.vol_gate_passed ? "pass" : "miss"}
                    </span>
                    {` (${fmtDiagPct(dg.pct_diff)} / ${fmtDiagPct(dg.vol)} vs ${fmtDiagPct(dg.vol_gate_threshold)})`}
                  </>
                );
              const entryGate = dg.strict_gate_passed === undefined
                ? "—"
                : (
                  <>
                    <span style={{ color: dg.strict_gate_passed ? "#3ddc84" : "#f5a623" }}>
                      {dg.strict_gate_passed ? "pass" : "miss"}
                    </span>
                    {` (${fmtDiagPct(dg.strict_diff)} vs ${fmtDiagPct(dg.strict_threshold)})`}
                  </>
                );
              return (
                <tr key={sym}>
                  <td className="mono">{sym}</td>
                  <td>{dg.trend ?? "flat"}</td>
                  <td className="mono">{fmtPrice(dg.price)}</td>
                  <td className="mono">{fmtPrice(dg.pred)}</td>
                  <td className="mono">{dg.pct_diff !== undefined ? fmtDiagPct(dg.pct_diff) : "—"}</td>
                  <td>{volGate}</td>
                  <td>{entryGate}</td>
                  <td style={{ color: dg.signal_fired ? "var(--green)" : undefined }}>{diagStageLabel(dg)}</td>
                  <td>{fmtTime(dg.ts)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {heartbeat && symbols.length === 0 && (
          <div className="empty-state">
            No diagnostic recorded yet — either the engine hasn't ticked since this panel was added, or
            hourly_task() hasn't run yet. Check the Engine Heartbeat above and the GitHub Actions run history.
          </div>
        )}
        <div className="hint" style={{ marginTop: 8, fontSize: 12, color: "var(--text-faint)" }}>
          &quot;Vol Gate&quot; and &quot;Entry Gate&quot; show the raw comparison the strategy makes every
          hour, whether or not it clears — a quiet stretch with both gates consistently missing by a small
          margin is the strategy correctly waiting for a bigger move, not a stuck/disconnected engine.
          Mechanics are identical to the validated walkforward.py backtest (see SPEC.md/DOCS.md) — nothing
          here is a reduced or temporary version of the real signal logic.
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Signal Diagnostics History</h3>
        <p className="hint" style={{ marginTop: -4 }}>
          Every hourly tick, per symbol, not just the latest — added so a quiet stretch (strategy correctly
          waiting) is visible as a trend rather than a single opaque snapshot. The solid line is how far price
          has actually moved from the UKF prediction each tick; the dashed line is how far it needs to move to
          clear the volatility gate — green dots mark ticks where a signal actually fired. Comparing this
          against a specific backtest run&apos;s own trade timing (Backtest page) is the next step here; for
          now this is the live side only.
        </p>
        {diagHistoryError && <div className="banner error">Could not reach the API ({diagHistoryError}).</div>}
        {diagHistory && diagHistory.length === 0 && (
          <div className="empty-state">No diagnostics history yet — it accumulates from this point forward.</div>
        )}
        {diagHistory && diagHistory.length > 0 && (
          <>
            {[...new Set(diagHistory.map((r) => r.symbol))].map((sym) => {
                const rows = diagHistory.filter((r) => r.symbol === sym);
                return (
                  <div key={sym} style={{ marginTop: 14 }}>
                    <div className="stat-label" style={{ marginBottom: 4 }}>
                      {sym} — last {rows.length} tick(s)
                    </div>
                    <DiagHistoryChart rows={rows} />
                    <details style={{ marginTop: 6 }}>
                      <summary className="hint" style={{ cursor: "pointer" }}>▼ Raw ticks</summary>
                      <table style={{ fontSize: 12, marginTop: 6 }}>
                        <thead>
                          <tr><th>As of</th><th>Trend</th><th>Stage</th><th>% Move</th><th>Vol Gate Thresh.</th></tr>
                        </thead>
                        <tbody>
                          {rows.slice(0, 20).map((r) => (
                            <tr key={r.id}>
                              <td>{fmtTime(r.ts)}</td>
                              <td>{r.trend ?? "flat"}</td>
                              <td style={{ color: r.signal_fired ? "var(--green)" : undefined }}>{diagStageLabel(r)}</td>
                              <td className="mono">{r.pct_diff !== null && r.pct_diff !== undefined ? `${(r.pct_diff * 100).toFixed(3)}%` : "—"}</td>
                              <td className="mono">{r.vol_gate_threshold !== null && r.vol_gate_threshold !== undefined ? `${(r.vol_gate_threshold * 100).toFixed(3)}%` : "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </details>
                  </div>
                );
              })}
          </>
        )}
      </div>

      <div className="card">
        <h3>Currently Open Positions</h3>
        {liveError ? (
          <div className="banner error">Could not reach the API ({liveError}).</div>
        ) : (
          <>
            <table>
              <thead>
                <tr>
                  <th>Symbol</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th>
                  <th>Qty</th><th>Capital</th><th>Max Loss</th><th>Mode</th><th>Opened</th>
                </tr>
              </thead>
              <tbody>
                {live && live.signals.map((s, i) => (
                  <tr key={i}>
                    <td className="mono">{s.symbol}</td>
                    <td className={s.side === "BUY" ? "side-buy" : "side-sell"}>{s.side}</td>
                    <td className="mono">{fmtPrice(s.entry_price)}</td>
                    <td className="mono">{fmtPrice(s.sl_price)}</td>
                    <td className="mono">{fmtPrice(s.tp_price)}</td>
                    <td className="mono">{s.qty ?? "—"}</td>
                    <td className="mono">{fmtMoney(s.entry_capital)}</td>
                    <td className="mono">{fmtMoney(s.expected_max_loss)}</td>
                    <td>{s.mode}</td>
                    <td>{fmtTime(s.opened_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {live && live.signals.length === 0 && (
              <div className="empty-state">No open positions. The last hourly cycle generated no entry, or trading is currently OFF.</div>
            )}
          </>
        )}
      </div>
    </Layout>
  );
}
