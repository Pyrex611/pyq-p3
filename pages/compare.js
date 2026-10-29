import { useEffect, useState } from "react";
import Layout from "../components/Layout";
import { apiGet, fmtPct, fmtTime, pnlClass } from "../lib/api";

function StatTile({ label, value, cls }) {
  return (
    <div>
      <div className={`stat-value ${cls || ""}`}>{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

export default function Compare() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [daily, setDaily] = useState(null);
  const [dailyError, setDailyError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/compare")
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(e.message); });
    apiGet("/compare/daily")
      .then((d) => { if (!cancelled) setDaily(d); })
      .catch((e) => { if (!cancelled) setDailyError(e.message); });
    return () => { cancelled = true; };
  }, []);

  const live = (data && data.live) || {};
  const wf = data && data.latest_walkforward_run;
  const wfMetrics = (wf && wf.metrics) || {};
  const retPct = wfMetrics["Total Return (%)"];
  const retFrac = retPct !== undefined && retPct !== null ? retPct / 100 : null;

  // Merge both sides' day buckets into one sorted list of dates for the
  // table below — any date present on either side gets a row (missing
  // on one side just renders as "—" for that side's columns).
  const liveByDay = (daily && daily.live_by_day) || {};
  const backtestByDay = (daily && daily.backtest_by_day) || {};
  const allDates = Array.from(new Set([...Object.keys(liveByDay), ...Object.keys(backtestByDay)])).sort();

  return (
    <Layout
      title="Compare"
      footer={'"Live" figures respect the Settings page\'s signals baseline, same as the Dashboard — see Settings → "Signals Baseline" if these numbers look like they include pre-fix history.'}
    >
      <div className="banner warn">
        Aggregate (whole-history) comparison below; the day-by-day table further down bucket-matches both
        sides by calendar day — see that section for why day-granularity, not a 1:1 trade match.
      </div>
      {error && <div className="banner error">Could not reach the API ({error}).</div>}

      <div className="grid cols-2">
        <div className="card">
          <h3>Live (Signal-Only Tracking)</h3>
          <div className="grid cols-2" style={{ gap: 14 }}>
            {!data ? (
              <div className="empty-state">Loading…</div>
            ) : (
              <>
                <StatTile label="Avg. Return" value={fmtPct(live.avg_pnl_pct)} cls={pnlClass(live.avg_pnl_pct)} />
                <StatTile label="Win Rate" value={live.win_rate_pct != null ? `${live.win_rate_pct.toFixed(1)}%` : "—"} />
                <StatTile label="Closed Trades" value={live.trade_count ?? "—"} />
                <StatTile label="Wins / Losses" value={`${live.win_count ?? "—"} / ${live.loss_count ?? "—"}`} />
              </>
            )}
          </div>
          <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>
            {data && data.signals_baseline_at
              ? `Since baseline reset: ${fmtTime(data.signals_baseline_at)}`
              : data ? "Counting all signal history (no baseline set)." : ""}
          </p>
        </div>
        <div className="card">
          <h3>Latest Walkforward Backtest</h3>
          <div className="grid cols-2" style={{ gap: 14 }}>
            {!data ? (
              <div className="empty-state">Loading…</div>
            ) : !wf ? (
              <div className="empty-state">No completed walkforward run yet — use the Backtest page.</div>
            ) : (
              <>
                <StatTile label="Total Return" value={retFrac !== null ? fmtPct(retFrac) : "—"} cls={pnlClass(retFrac)} />
                <StatTile label="Win Rate" value={wfMetrics["Win Rate (%)"] !== undefined ? `${wfMetrics["Win Rate (%)"]}%` : "—"} />
                <StatTile label="Total Trades" value={wfMetrics["Total Trades"] ?? wfMetrics["trade_count"] ?? "—"} />
                <StatTile label="Run" value={`#${wf.id}`} />
              </>
            )}
          </div>
          <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>{wf ? `Finished ${fmtTime(wf.finished_at)}` : ""}</p>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>Day-by-Day (per-trade foundation)</h3>
        <p className="hint" style={{ marginTop: -4 }}>
          Added 2026-10-29, built on the per-trade backtest logging shipped 2026-10-28
          (<code className="mono">pyquant_backtest_trades</code>). Not a 1:1 trade match — live ticks on a
          ~5-minute cadence against real-time bars while the backtest walks fixed historical windows, so
          exact timestamps never line up — instead both sides are bucketed by UTC calendar day and compared
          trade count / win rate / avg return per day, from run <code className="mono">#{daily ? daily.run_id ?? "—" : "—"}</code>.
          &quot;Win&quot; on the backtest side is approximated as a positive <code className="mono">pnl_pct</code> trade
          (the live side uses the exact <code className="mono">closed_tp</code> status); treat the win-rate column
          as directionally comparable, not bit-for-bit identical in definition.
        </p>
        {dailyError && <div className="banner error">Could not reach the API ({dailyError}).</div>}
        {daily && allDates.length === 0 && (
          <div className="empty-state">No overlapping data yet — run a backtest and let the live engine log some closed signals.</div>
        )}
        {daily && allDates.length > 0 && (
          <div className="daily-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Live Trades</th><th>Live Win %</th><th>Live Avg %</th>
                  <th>Backtest Trades</th><th>Backtest Win %</th><th>Backtest Avg %</th>
                  <th>Avg % Δ (Live − Backtest)</th>
                </tr>
              </thead>
              <tbody>
                {allDates.map((day) => {
                  const l = liveByDay[day];
                  const b = backtestByDay[day];
                  const delta = l && b && l.avg_pnl_pct !== null && b.avg_pnl_pct !== null
                    ? l.avg_pnl_pct - b.avg_pnl_pct
                    : null;
                  return (
                    <tr key={day}>
                      <td className="mono">{day}</td>
                      <td className="mono">{l ? l.trade_count : "—"}</td>
                      <td className="mono">{l && l.win_rate_pct !== null ? `${l.win_rate_pct.toFixed(1)}%` : "—"}</td>
                      <td className={`mono ${l ? pnlClass(l.avg_pnl_pct) : ""}`}>{l && l.avg_pnl_pct !== null ? fmtPct(l.avg_pnl_pct) : "—"}</td>
                      <td className="mono">{b ? b.trade_count : "—"}</td>
                      <td className="mono">{b && b.win_rate_pct !== null ? `${b.win_rate_pct.toFixed(1)}%` : "—"}</td>
                      <td className={`mono ${b ? pnlClass(b.avg_pnl_pct) : ""}`}>{b && b.avg_pnl_pct !== null ? fmtPct(b.avg_pnl_pct) : "—"}</td>
                      <td className={`mono ${delta !== null ? (delta >= 0 ? "delta-pos" : "delta-neg") : ""}`}>
                        {delta !== null ? fmtPct(delta) : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Layout>
  );
}
