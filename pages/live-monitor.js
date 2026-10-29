import { useEffect, useState } from "react";
import Layout from "../components/Layout";
import { apiGet, fmtPrice, fmtTime, fmtMoney } from "../lib/api";

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

  useEffect(() => {
    let cancelled = false;
    apiGet("/heartbeat")
      .then((h) => { if (!cancelled) setHeartbeat(h); })
      .catch((e) => { if (!cancelled) setHbError(e.message); });
    apiGet("/signals/open")
      .then((d) => { if (!cancelled) setLive(d); })
      .catch((e) => { if (!cancelled) setLiveError(e.message); });
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
                  <td>{dg.signal_fired ? "YES" : (dg.stage_reached === "exception" ? `error: ${dg.error ?? ""}` : "no")}</td>
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
