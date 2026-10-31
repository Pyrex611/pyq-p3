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

  useEffect(() => {
    apiGet("/config").then(setCfg).catch((e) => setCfgError(e.message));
    loadBaseline();
  }, []);

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
