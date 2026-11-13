import { useEffect, useState } from "react";
import Layout from "../components/Layout";
import { apiGet, apiPost, fmtTime } from "../lib/api";

// Mirrors pyquant_signal_math.LEVERAGE_DEFAULT (10) — the Dashboard's
// Avg. Return recomputes from this when an override is set. Kept in
// sync with the same constant in pages/index.js.
const SYSTEM_LEVERAGE = 10;
const LEVERAGE_OVERRIDE_KEY = "pyquant_leverage_override";

const EXCHANGE_KEYS = [
  ["ALPACA_API_KEY", "Alpaca API Key"],
  ["ALPACA_SECRET_KEY", "Alpaca Secret Key"],
  ["BYBIT_API_KEY", "Bybit API Key"],
  ["BYBIT_SECRET_KEY", "Bybit Secret Key"],
  ["BINANCE_API_KEY", "Binance API Key (mainnet)"],
  ["BINANCE_SECRET_KEY", "Binance Secret Key (mainnet)"],
  ["BINANCE_TESTNET_API_KEY", "Binance API Key (testnet / paper)"],
  ["BINANCE_TESTNET_SECRET_KEY", "Binance Secret Key (testnet / paper)"],
];
const OTHER_KEYS = [
  ["TELEGRAM_TOKEN", "Telegram Bot Token"],
  ["TELEGRAM_CHAT_ID", "Telegram Chat ID"],
  ["DATABASE_URL", "Postgres DATABASE_URL"],
];

function ConfigRows({ cfg, keys }) {
  return keys.map(([key, label]) => {
    const set = !!(cfg && cfg[key]);
    return (
      <div className="config-row" key={key}>
        <span className="config-key">
          <span className={`dot ${set ? "set" : "unset"}`}></span>{label}
        </span>
        <span className="mono" style={{ color: set ? "var(--green)" : "var(--text-faint)", fontSize: 12 }}>
          {set ? "CONFIGURED" : "NOT SET"}
        </span>
      </div>
    );
  });
}

export default function Settings() {
  const [cfg, setCfg] = useState(null);
  const [cfgError, setCfgError] = useState(null);
  const [baseline, setBaseline] = useState(undefined); // undefined = loading, null = no baseline, string = set
  const [baselineError, setBaselineError] = useState(null);
  const [leverageInput, setLeverageInput] = useState("");
  const [sigSource, setSigSource] = useState(null);
  const [sigSourceError, setSigSourceError] = useState(null);
  const [sigSourceBusy, setSigSourceBusy] = useState(false);
  const [sigSourceWarning, setSigSourceWarning] = useState(null);
  const [trigger, setTrigger] = useState(null);
  const [triggerError, setTriggerError] = useState(null);
  const [triggerBusy, setTriggerBusy] = useState(false);
  const [anchor, setAnchor] = useState(null);
  const [anchorError, setAnchorError] = useState(null);
  const [anchorBusy, setAnchorBusy] = useState(false);
  const [sigTrigger, setSigTrigger] = useState(null);
  const [sigTriggerError, setSigTriggerError] = useState(null);
  const [sigTriggerBusy, setSigTriggerBusy] = useState(false);

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(LEVERAGE_OVERRIDE_KEY);
      if (stored) setLeverageInput(stored);
    } catch { /* localStorage unavailable — override just stays off */ }
  }, []);

  function updateLeverageInput(v) {
    setLeverageInput(v);
    try {
      if (v) window.localStorage.setItem(LEVERAGE_OVERRIDE_KEY, v);
      else window.localStorage.removeItem(LEVERAGE_OVERRIDE_KEY);
    } catch { /* non-fatal — the Dashboard just won't pick up the override */ }
  }

  async function loadBaseline() {
    try {
      const d = await apiGet("/signals/baseline");
      setBaseline(d.reset_at);
      setBaselineError(null);
    } catch (e) {
      setBaselineError(e.message);
    }
  }

  async function loadSigSource() {
    try {
      const d = await apiGet("/signal-source");
      setSigSource(d);
      setSigSourceError(null);
    } catch (e) {
      setSigSourceError(e.message);
    }
  }

  async function loadTrigger() {
    try {
      const d = await apiGet("/optimizer/trigger");
      setTrigger(d);
      setTriggerError(null);
    } catch (e) {
      setTriggerError(e.message);
    }
  }

  async function loadAnchor() {
    try {
      const d = await apiGet("/anchor/trigger");
      setAnchor(d);
      setAnchorError(null);
    } catch (e) {
      setAnchorError(e.message);
    }
  }

  async function loadSigTrigger() {
    try {
      const d = await apiGet("/signal/trigger");
      setSigTrigger(d);
      setSigTriggerError(null);
    } catch (e) {
      setSigTriggerError(e.message);
    }
  }

  useEffect(() => {
    apiGet("/config").then(setCfg).catch((e) => setCfgError(e.message));
    loadBaseline();
    loadSigSource();
    loadTrigger();
    loadAnchor();
    loadSigTrigger();
  }, []);

  async function updateSigSource(patch) {
    setSigSourceBusy(true);
    setSigSourceWarning(null);
    try {
      const d = await apiPost("/signal-source", patch);
      if (d.warning) setSigSourceWarning(d.warning);
      await loadSigSource();
    } catch (e) {
      alert("Could not update signal source settings: " + e.message);
    } finally {
      setSigSourceBusy(false);
    }
  }

  async function runOptimizerNow(which) {
    const label = which === "both" ? "BOTH optimizers" : which.toUpperCase();
    if (!confirm(
      `Queue ${label} to run on the next cron tick (within ~5 minutes)?` +
      (which === "both"
        ? " They will run on two separate ticks, never together, so this takes up to ~10 minutes total."
        : "")
    )) return;
    setTriggerBusy(true);
    try {
      await apiPost("/optimizer/trigger", { which });
      await loadTrigger();
    } catch (e) {
      alert("Could not queue the optimizer run: " + e.message);
    } finally {
      setTriggerBusy(false);
    }
  }

  async function runAnchorNow() {
    if (!confirm("Re-anchor the 1H UKFs now (on the next cron tick, within ~5 minutes)?")) return;
    setAnchorBusy(true);
    try {
      await apiPost("/anchor/trigger", {});
      await loadAnchor();
    } catch (e) {
      alert("Could not queue the manual anchor: " + e.message);
    } finally {
      setAnchorBusy(false);
    }
  }

  async function runSignalNow() {
    if (!confirm(
      "Run hourly signal generation now (on the next cron tick, within ~5 minutes)? " +
      "This runs in addition to the normal hourly tick and never places a real order " +
      "while SIGNAL_ONLY_MODE is on."
    )) return;
    setSigTriggerBusy(true);
    try {
      await apiPost("/signal/trigger", {});
      await loadSigTrigger();
    } catch (e) {
      alert("Could not queue the manual signal run: " + e.message);
    } finally {
      setSigTriggerBusy(false);
    }
  }

  async function resetBaseline() {
    if (!confirm("Dashboard figures will only count signals opened from this moment on. Continue?")) return;
    try {
      await apiPost("/signals/baseline/reset", {});
      await loadBaseline();
    } catch (e) {
      alert("Could not reset the baseline: " + e.message);
    }
  }

  async function clearBaseline() {
    try {
      await apiPost("/signals/baseline/clear", {});
      await loadBaseline();
    } catch (e) {
      alert("Could not clear the baseline: " + e.message);
    }
  }

  return (
    <Layout
      title="API Keys & Settings"
      footer="To rotate a key, update it in the Vercel dashboard (or the orchestra VPS's .env for the trading engine itself) and redeploy/restart — this page is read-only."
    >
      <div className="banner warn">
        This screen shows whether a credential is configured in the API&apos;s own Vercel environment — never
        the actual values. Keys are managed in the Vercel project&apos;s Environment Variables, not here.
      </div>
      {cfgError && <div className="banner error">Could not reach the API ({cfgError}).</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Leverage Override</h3>
        <label>
          Leverage ×<br />
          <input
            type="number" min="1" step="0.5" placeholder={String(SYSTEM_LEVERAGE)} style={{ width: 90 }}
            value={leverageInput}
            onChange={(e) => updateLeverageInput(e.target.value)}
          />
        </label>
        <button type="button" className="btn" style={{ marginLeft: 8 }} onClick={() => updateLeverageInput("")}>
          Reset to system default
        </button>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Signal Generation Source &amp; Shadow Comparison</h3>
        <p className="hint" style={{ marginTop: -4 }}>
          Live trading can source its strict/volatility parameters from either optimizer. The gate math
          itself (generate_signal()/diagnose_gates()) is identical either way — only WHICH strict/stricts
          values feed it differs. <strong>Walkforward</strong> is the same optimizer walkforward.py&apos;s own
          backtests use; <strong>P4_SEARCH</strong> is today&apos;s existing production source, a separate,
          independently-maintained duplicate. Switching sources changes what real orders are sized/fired
          on — not a cosmetic setting.
        </p>
        {sigSourceError && <div className="banner error">Could not load ({sigSourceError}).</div>}
        {sigSourceWarning && <div className="banner warn">{sigSourceWarning}</div>}
        {sigSource && (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
              <span style={{ fontSize: 13, color: "var(--text-muted)", minWidth: 170 }}>
                Live trading uses:
              </span>
              <div className="mode-switch">
                <button
                  type="button"
                  className={sigSource.live_signal_source === "p4_search" ? "active" : ""}
                  disabled={sigSourceBusy}
                  onClick={() => updateSigSource({ live_signal_source: "p4_search" })}
                >
                  P4_SEARCH
                </button>
                <button
                  type="button"
                  className={sigSource.live_signal_source === "walkforward" ? "active" : ""}
                  disabled={sigSourceBusy}
                  onClick={() => updateSigSource({ live_signal_source: "walkforward" })}
                >
                  WALKFORWARD
                </button>
              </div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
              <span style={{ fontSize: 13, color: "var(--text-muted)", minWidth: 170 }}>
                Shadow comparison (both run, non-selected one recorded only):
              </span>
              <div className="mode-switch">
                <button
                  type="button"
                  className={!sigSource.shadow_comparison_enabled ? "active off" : ""}
                  disabled={sigSourceBusy}
                  onClick={() => updateSigSource({ shadow_comparison_enabled: false })}
                >
                  OFF
                </button>
                <button
                  type="button"
                  className={sigSource.shadow_comparison_enabled ? "active" : ""}
                  disabled={sigSourceBusy}
                  onClick={() => updateSigSource({ shadow_comparison_enabled: true })}
                >
                  ON
                </button>
              </div>
            </div>
            <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>
              walkforward_optimal_params: {sigSource.walkforward_params_available
                ? "available (shadow optimizer has run at least once)"
                : "not yet available — the shadow optimizer hasn't completed a run"}
            </div>
            <p className="hint" style={{ marginTop: 10, marginBottom: 0 }}>
              Turning shadow comparison ON makes the daily optimizer also run walkforward.py&apos;s own
              search every night, staggered to ~01:30 UTC (30 min after the real optimizer&apos;s own
              ~01:00 UTC run — own run-guard each, never sharing one GitHub Actions job; see PLAN.md&apos;s
              2026-11-04 note). Until it has completed at least once, switching live trading to WALKFORWARD
              has no effect — SignalGenerator.load_params() falls back to P4_SEARCH&apos;s params rather
              than trade on nothing. Both sources&apos; gate readings, once available, show up tagged on
              the Live Monitor/Compare pages so you can see which one tracks the backtest more closely.
            </p>

            <div style={{ marginTop: 14, paddingTop: 14, borderTop: "1px solid var(--border-soft)" }}>
              <div className="stat-label" style={{ marginBottom: 8 }}>Run Optimizer Now</div>
              {triggerError && <div className="banner error">Could not load ({triggerError}).</div>}
              {trigger && (
                <>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
                    <button type="button" className="btn" disabled={triggerBusy} onClick={() => runOptimizerNow("p4_search")}>
                      Run P4_SEARCH Now
                    </button>
                    <button type="button" className="btn" disabled={triggerBusy} onClick={() => runOptimizerNow("walkforward")}>
                      Run WALKFORWARD Now
                    </button>
                    <button type="button" className="btn" disabled={triggerBusy} onClick={() => runOptimizerNow("both")}>
                      Run Both Now
                    </button>
                  </div>
                  <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.6 }}>
                    <div>
                      P4_SEARCH: {trigger.pending?.p4_search ? "queued — runs within ~5 min" :
                        trigger.last_run?.p4_search ? `last ran ${fmtTime(trigger.last_run.p4_search.finished_at || trigger.last_run.p4_search.started_at)}` :
                        "no run recorded yet"}
                    </div>
                    <div>
                      WALKFORWARD: {trigger.pending?.walkforward ? "queued — runs within ~5 min" :
                        trigger.last_run?.walkforward ? `last ran ${fmtTime(trigger.last_run.walkforward.finished_at || trigger.last_run.walkforward.started_at)}` :
                        "no run recorded yet"}
                    </div>
                  </div>
                </>
              )}
              <p className="hint" style={{ marginTop: 8, marginBottom: 0 }}>
                Each manual run is an EXTRA run, on top of (not instead of) the normal automatic schedule
                above — it won&apos;t stop tonight&apos;s scheduled run from also happening. Requesting
                &quot;Both&quot; queues them one tick apart (~5 min) rather than together, for the same
                timeout-safety reason the automatic schedule staggers them by 30 minutes.
              </p>
            </div>

            <div style={{ marginTop: 14, paddingTop: 14, borderTop: "1px solid var(--border-soft)" }}>
              <div className="stat-label" style={{ marginBottom: 8 }}>Anchor Now</div>
              {anchorError && <div className="banner error">Could not load ({anchorError}).</div>}
              <button type="button" className="btn" disabled={anchorBusy} onClick={runAnchorNow}>
                ⚓ Re-anchor UKFs Now
              </button>
              {anchor && (
                <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.6, marginTop: 8 }}>
                  {anchor.pending
                    ? "queued — runs within ~5 min"
                    : anchor.last_heartbeat?.ts
                    ? `last anchor ${anchor.last_heartbeat.status === "failed" ? "FAILED" : "ok"} at ${fmtTime(anchor.last_heartbeat.ts)}` +
                      (anchor.last_heartbeat.triggered_by === "manual" ? " (manual)" : " (automatic)")
                    : "no anchor recorded yet"}
                </div>
              )}
              <p className="hint" style={{ marginTop: 8, marginBottom: 0 }}>
                Re-anchors the 1H UKFs (fresh 30-day fit) right now, same as the once-a-day
                00:01 UTC automatic retrain — an EXTRA one-off run, on top of (not instead of) tonight&apos;s
                scheduled retrain. Useful for confirming a fix to the UKF right away rather than waiting for
                the next daily cycle.
              </p>
            </div>

            <div style={{ marginTop: 14, paddingTop: 14, borderTop: "1px solid var(--border-soft)" }}>
              <div className="stat-label" style={{ marginBottom: 8 }}>Generate Signal Now</div>
              {sigTriggerError && <div className="banner error">Could not load ({sigTriggerError}).</div>}
              <button type="button" className="btn" disabled={sigTriggerBusy} onClick={runSignalNow}>
                ⚡ Generate Signal Now
              </button>
              {sigTrigger && (
                <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.6, marginTop: 8 }}>
                  {sigTrigger.pending
                    ? "queued — runs within ~5 min"
                    : sigTrigger.last_cron_tick?.ts
                    ? `last cron tick at ${fmtTime(sigTrigger.last_cron_tick.ts)}`
                    : "no cron tick recorded yet"}
                </div>
              )}
              <p className="hint" style={{ marginTop: 8, marginBottom: 0 }}>
                Runs hourly signal generation (and its diagnostics) right now, as an EXTRA one-off run on
                top of (not instead of) the normal hourly tick. Added to confirm the fresh-from-scratch
                UKF bypass (2026-11-13) without waiting up to an hour — never places a real order while
                SIGNAL_ONLY_MODE is on.
              </p>
            </div>
          </>
        )}
      </div>

      <div className="grid cols-2">
        <div className="card">
          <h3>Exchange &amp; Data Credentials</h3>
          <div><ConfigRows cfg={cfg} keys={EXCHANGE_KEYS} /></div>
        </div>
        <div className="card">
          <h3>Notifications &amp; Persistence</h3>
          <div><ConfigRows cfg={cfg} keys={OTHER_KEYS} /></div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>Signals Baseline (&quot;Fresh Start&quot;)</h3>
        <p className="hint" style={{ marginTop: -4 }}>
          The Dashboard&apos;s figures (open positions, recently closed, avg. return) only count signals opened
          on or after this timestamp. Nothing is deleted — the Signals and Compare pages always show full
          history regardless. Use this after a change to the tracking/PnL logic makes older numbers not
          comparable to new ones (e.g. the 2026-10-25 TP/SL-tracking fix).
        </p>
        <div className="mono" style={{ fontSize: 13, marginBottom: 10 }}>
          {baselineError
            ? `Could not load (${baselineError}).`
            : baseline === undefined
              ? "Checking…"
              : baseline
                ? `Counting signals opened on/after ${fmtTime(baseline)}.`
                : "No baseline set — counting all signals (original behavior)."}
        </div>
        <button type="button" className="btn primary" onClick={resetBaseline}>Start Fresh From Now</button>
        <button type="button" className="btn" style={{ marginLeft: 8 }} onClick={clearBaseline}>Clear (count everything again)</button>
      </div>
    </Layout>
  );
}
