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

  return (
    <>
      <button type="button" className={mode === "LIVE" ? "active" : ""} disabled>LIVE</button>
      <button type="button" className={mode === "PAPER" ? "active" : ""} disabled>PAPER</button>
      <button type="button" className={!enabled ? "off active" : ""} disabled={busy} onClick={toggle}>
        {enabled ? "TURN OFF" : "OFF (tap to resume)"}
      </button>
    </>
  );
}
