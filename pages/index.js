import { useEffect, useMemo, useState } from "react";
import Layout from "../components/Layout";
import { apiGet, fmtMoney, fmtPct, fmtPrice, fmtTime, pnlClass, statusLabel, rawPct } from "../lib/api";

// Mirrors pyquant_signal_math.LEVERAGE_DEFAULT (10) — every stored
// realized_pnl_pct value was computed with exactly this leverage baked
// in (pyq_p4.py's _check_signal_only_closes() calls outcome_pct() with
// no explicit leverage arg, so it always uses this default — confirmed
// by reading that call site directly, not assumed). Hardcoded here
// rather than fetched: it's a stable, rarely-changed constant, and
// adding a round trip just to read one number isn't worth it. If it's
// ever changed in pyquant_signal_math.py, update this too.
const SYSTEM_LEVERAGE = 10;

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [leverageInput, setLeverageInput] = useState("");

  useEffect(() => {
    let cancelled = false;
    apiGet("/dashboard")
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, []);

  const lev = (() => {
    const v = parseFloat(leverageInput);
    return v && v > 0 ? v : SYSTEM_LEVERAGE;
  })();
  const usingOverride = lev !== SYSTEM_LEVERAGE;

  const heartbeat = useMemo(() => {
    if (!data) return null;
    const age = data.cron_age_seconds;
    if (age === null || age === undefined) {
      return {
        color: "var(--text-faint)",
        text: "No cron tick recorded yet — either the engine hasn't run since this field was added, or it isn't running. Check the GitHub Actions run history.",
      };
    }
    const mins = Math.floor(age / 60);
    if (age < 360) {
      return { color: "#3ddc84", text: `● Live — last tick ${mins < 1 ? "under a minute" : mins + " min"} ago` };
    }
    if (age < 900) {
      return { color: "#f5a623", text: `● Stale — last tick ${mins} min ago (expected every ~5 min)` };
    }
    return { color: "#e5484d", text: `● Not reporting — last tick ${mins} min ago. Check the GitHub Actions run history.` };
  }, [data]);

  // Recomputed (if a leverage override is active) from the SAME rows the
  // backend's avg_pnl_pct_recent covers — but only the ones also
  // returned in recent_closed (10 of up to 200), since that's all this
  // page has entry/outcome prices for. Labeled accordingly so the scope
  // difference is never silently hidden.
  const closedWithPct = useMemo(() => {
    if (!data) return [];
    return data.recent_closed
      .map((s) => rawPct(s.side, s.entry_price, s.outcome_price))
      .filter((v) => v !== null);
  }, [data]);
  const avgRaw = closedWithPct.length
    ? closedWithPct.reduce((a, b) => a + b, 0) / closedWithPct.length
    : null;
  const avgLeveraged = avgRaw !== null ? avgRaw * lev : null;

  let pnlValue = "—", pnlLabel = "last 200 logged signals", pnlCls = "";
  if (data) {
    if (usingOverride && avgLeveraged !== null) {
      pnlValue = fmtPct(avgLeveraged);
      pnlCls = pnlClass(avgLeveraged);
      pnlLabel = `at ${lev}× leverage, last ${closedWithPct.length} shown below`;
    } else if (data.avg_pnl_pct_recent !== null && data.avg_pnl_pct_recent !== undefined) {
      pnlValue = fmtPct(data.avg_pnl_pct_recent);
      pnlCls = pnlClass(data.avg_pnl_pct_recent);
      pnlLabel = `avg. leveraged return (${SYSTEM_LEVERAGE}×), last 200 logged signals`;
    } else {
      pnlValue = fmtMoney(data.realized_pnl_recent);
      pnlCls = pnlClass(data.realized_pnl_recent);
      pnlLabel = "last 200 logged signals (USD — no % data yet)";
    }
  }

  return (
    <Layout title="Dashboard" footer="Data refreshes on page load. Trading toggle takes effect on the next scheduled hourly cycle.">
      {error && (
        <div className="banner error">
          Could not reach the API ({error}). Check that the backend is deployed and PYQUANT_API_BASE / the /api rewrite is configured.
        </div>
      )}

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Engine Heartbeat</h3>
        <div className="stat-label" style={heartbeat ? { color: heartbeat.color } : undefined}>
          {heartbeat ? heartbeat.text : "Checking…"}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Leverage</h3>
        <label>
          Leverage ×<br />
          <input
            type="number" min="1" step="0.5" placeholder="10" style={{ width: 90 }}
            value={leverageInput}
            onChange={(e) => setLeverageInput(e.target.value)}
          />
        </label>
        <button type="button" className="btn" style={{ marginLeft: 8 }} onClick={() => setLeverageInput("")}>
          Reset to system default
        </button>
      </div>

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <div className="card">
          <h3>Open Positions</h3>
          <div className="stat-value">{data ? data.open_positions.length : "—"}</div>
          <div className="stat-label">currently monitored</div>
        </div>
        <div className="card">
          <h3>Avg. Return (recent)</h3>
          <div className={`stat-value ${pnlCls}`}>{pnlValue}</div>
          <div className="stat-label">{pnlLabel}</div>
        </div>
        <div className="card">
          <h3>Trading</h3>
          <div className="stat-value">{data ? (data.trading_enabled ? "ON" : "OFF") : "—"}</div>
          <div className="stat-label">{data ? `${data.mode.toUpperCase()} mode` : "—"}</div>
        </div>
        <div className="card">
          <h3>Last Run</h3>
          <div className="stat-value">{data && data.last_run ? data.last_run.run_type : "—"}</div>
          <div className="stat-label">
            {data && data.last_run ? fmtTime(data.last_run.finished_at) : "no runs logged yet"}
          </div>
        </div>
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h3>Open Positions</h3>
          <table>
            <thead>
              <tr><th>Symbol</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Opened</th></tr>
            </thead>
            <tbody>
              {data && data.open_positions.map((s, i) => (
                <tr key={i}>
                  <td className="mono">{s.symbol}</td>
                  <td className={s.side === "BUY" ? "side-buy" : "side-sell"}>{s.side}</td>
                  <td className="mono">{fmtPrice(s.entry_price)}</td>
                  <td className="mono">{fmtPrice(s.sl_price)}</td>
                  <td className="mono">{fmtPrice(s.tp_price)}</td>
                  <td>{fmtTime(s.opened_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {data && data.open_positions.length === 0 && (
            <div className="empty-state">No open positions right now.</div>
          )}
        </div>
        <div className="card">
          <h3>Recently Closed</h3>
          <table>
            <thead>
              <tr><th>Symbol</th><th>Side</th><th>Outcome</th><th>PnL</th><th>Closed</th></tr>
            </thead>
            <tbody>
              {data && data.recent_closed.map((s, i) => {
                const raw = rawPct(s.side, s.entry_price, s.outcome_price);
                const displayPct = usingOverride && raw !== null
                  ? raw * lev
                  : (s.realized_pnl_pct !== null && s.realized_pnl_pct !== undefined ? s.realized_pnl_pct : null);
                return (
                  <tr key={i}>
                    <td className="mono">{s.symbol}</td>
                    <td className={s.side === "BUY" ? "side-buy" : "side-sell"}>{s.side}</td>
                    <td><span className={`pill ${s.status}`}>{statusLabel(s.status)}</span></td>
                    <td className={pnlClass(displayPct ?? s.realized_pnl)}>
                      {displayPct !== null ? fmtPct(displayPct) : fmtMoney(s.realized_pnl)}
                    </td>
                    <td>{fmtTime(s.closed_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {data && data.recent_closed.length === 0 && (
            <div className="empty-state">No closed signals logged yet.</div>
          )}
        </div>
      </div>
    </Layout>
  );
}
