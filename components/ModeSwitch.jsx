import { useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";

/**
 * Renders the LIVE/PAPER/OFF switch in the topbar and wires the OFF
 * toggle to POST /api/status. LIVE/PAPER are shown as the orchestra's
 * current configured mode (informational — see api/index.py's
 * /api/status docstring for why switching exchange credentials isn't
 * done from here); OFF is the one real, live control.
 *
 * Ported from web/assets/app.js's mountModeSwitch()/renderModeSwitch()
 * (2026-10-29 Next.js migration) — same behavior, now a React component
 * instead of a DOM-mutation function, mounted fresh on every page via
 * Layout rather than once per full page load.
 */
export default function ModeSwitch() {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiGet("/status")
      .then((s) => { if (!cancelled) setStatus(s); })
      .catch(() => { if (!cancelled) setError(true); });
    return () => { cancelled = true; };
  }, []);

  if (error) {
    return <span style={{ color: "var(--text-faint)", fontSize: 12 }}>status unavailable</span>;
  }
  if (!status) return null;

  const mode = status.mode === "paper" ? "PAPER" : "LIVE";
  const enabled = status.trading_enabled;
  const signalOnly = !!status.signal_only_mode;

  async function toggle() {
    setBusy(true);
    try {
      const next = await apiPost("/status", { enabled: !enabled });
      setStatus(next);
    } catch (e) {
      alert("Could not update trading switch: " + e.message);
    } finally {
      setBusy(false);
    }
  }

  // 2026-11-03: these two were always display-only by design (see this
  // file's own module docstring + api/index.py's GET /api/status
  // docstring) — clicking can't switch exchange credentials, because
  // doing that here would break the trust boundary (the API never holds
  // Binance keys). That read as "the toggle is stuck on LIVE" from the
  // owner's side with nothing explaining why. PYQUANT_MODE has to be
  // set as BOTH a Vercel env var (this API) and a GitHub Actions repo
  // variable (the orchestra) — see VERCEL_DEPLOY.md / Part C of
  // GITHUB_ACTIONS_SETUP.md — and this API defaults to "live" if its own
  // copy was never set, even when the orchestra's copy really is
  // "paper". The title tooltip below says this instead of nothing.
  const modeTooltip =
    "Display-only — set by the PYQUANT_MODE environment variable, in both " +
    "Vercel (this API) and the GitHub Actions orchestra separately. " +
    "Changing it here isn't possible: the API never holds exchange " +
    "credentials, so it can't switch them. See VERCEL_DEPLOY.md / " +
    "GITHUB_ACTIONS_SETUP.md Part C.";

  return (
    <>
      <button type="button" className={mode === "LIVE" ? "active" : ""} disabled title={modeTooltip}>LIVE</button>
      <button type="button" className={mode === "PAPER" ? "active" : ""} disabled title={modeTooltip}>PAPER</button>
      <button type="button" className={!enabled ? "off active" : ""} disabled={busy} onClick={toggle}>
        {enabled ? "TURN OFF" : "OFF (tap to resume)"}
      </button>
      <span
        title={
          signalOnly
            ? "SIGNAL_ONLY_MODE is on — the engine generates/logs/simulates signals only. " +
              "Zero Binance API calls happen right now, regardless of LIVE/PAPER or the switch above."
            : "SIGNAL_ONLY_MODE is off — real Binance order calls can happen (testnet only while PAPER, mainnet while LIVE)."
        }
        style={{
          marginLeft: 8, fontSize: 10, letterSpacing: "0.04em", fontWeight: 600,
          padding: "3px 7px", borderRadius: 999, cursor: "default",
          background: signalOnly ? "var(--green-glow)" : "var(--amber-glow)",
          color: signalOnly ? "var(--green)" : "var(--amber)",
          border: `1px solid ${signalOnly ? "var(--green)" : "var(--amber)"}`,
        }}
      >
        {signalOnly ? "SIGNAL-ONLY · NO EXCHANGE CALLS" : "EXCHANGE CALLS ENABLED"}
      </span>
    </>
  );
}
