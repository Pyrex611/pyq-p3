import React, { useRef } from "react";

const OPTUNA_TRIAL_OPTIONS = [10, 20, 30, 50, 70, 100, 150, 200, 250, 300, 400, 500];
const LEVERAGE_OPTIONS = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50];

export default function BacktestForm({
  form,
  setForm,
  bounds,
  onInitiateRun,
  onSyncKaggle,
  kaggleUrl,
  statusText,
}) {
  const startDateInputRef = useRef(null);
  const endDateInputRef = useRef(null);

  function setPreset(days) {
    setForm({
      ...form,
      walk_forward_days: String(days),
      start_date: "",
      end_date: "",
    });
  }

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h3 style={{ margin: 0 }}>Configure Walk-Forward Simulation</h3>
        <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
          Coverage Window: {bounds ? `${bounds.earliest_date} → ${bounds.latest_date}` : "Trailing 366 Days"}
        </span>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 14, marginBottom: 14 }}>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Duration Presets:</span>
        {["7", "14", "30", "60", "365"].map((d) => (
          <button
            key={d}
            type="button"
            className={`btn ${form.walk_forward_days === d && !form.start_date ? "primary" : ""}`}
            style={{ padding: "4px 10px", fontSize: 11 }}
            onClick={() => setPreset(d)}
          >
            {d === "365" ? "Full Dataset (366D)" : `${d} Days`}
          </button>
        ))}
      </div>

      <form onSubmit={(e) => { e.preventDefault(); onInitiateRun(); }}>
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "flex-end" }}>
          <label>
            Duration Days<br />
            <input
              type="number"
              min="1"
              max="366"
              style={{ width: 95, padding: "8px 10px", background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)" }}
              disabled={!!form.start_date}
              value={form.walk_forward_days}
              onChange={(e) => setForm({ ...form, walk_forward_days: e.target.value })}
            />
          </label>

          <label>
            Start Date<br />
            <button
              type="button"
              className={`date-picker-button ${form.start_date ? "active" : ""}`}
              onClick={() => {
                try { startDateInputRef.current?.showPicker(); }
                catch { startDateInputRef.current?.focus(); startDateInputRef.current?.click(); }
              }}
            >
              <span>📅</span>
              <span>{form.start_date || "Pick Start Date"}</span>
            </button>
            <input
              ref={startDateInputRef}
              type="date"
              style={{ position: "absolute", opacity: 0, pointerEvents: "none", width: 0, height: 0 }}
              min={bounds?.earliest_date}
              max={form.end_date || bounds?.latest_date}
              value={form.start_date}
              onChange={(e) => setForm({ ...form, start_date: e.target.value, walk_forward_days: "" })}
            />
          </label>

          <label>
            End Date<br />
            <button
              type="button"
              className={`date-picker-button ${form.end_date ? "active" : ""}`}
              disabled={!form.start_date}
              style={{ opacity: !form.start_date ? 0.45 : 1 }}
              onClick={() => {
                try { endDateInputRef.current?.showPicker(); }
                catch { endDateInputRef.current?.focus(); endDateInputRef.current?.click(); }
              }}
            >
              <span>📅</span>
              <span>{form.end_date || "Latest Available"}</span>
            </button>
            <input
              ref={endDateInputRef}
              type="date"
              style={{ position: "absolute", opacity: 0, pointerEvents: "none", width: 0, height: 0 }}
              min={form.start_date || bounds?.earliest_date}
              max={bounds?.latest_date}
              value={form.end_date}
              onChange={(e) => setForm({ ...form, end_date: e.target.value })}
            />
          </label>

          <label>
            Optuna Trials<br />
            <select
              className="dropdown-select"
              value={form.bayesian_trials}
              onChange={(e) => setForm({ ...form, bayesian_trials: e.target.value })}
            >
              {OPTUNA_TRIAL_OPTIONS.map((val) => (
                <option key={val} value={val}>{val} Trials</option>
              ))}
            </select>
          </label>

          <label>
            Leverage<br />
            <select
              className="dropdown-select"
              value={form.leverage}
              onChange={(e) => setForm({ ...form, leverage: e.target.value })}
            >
              {LEVERAGE_OPTIONS.map((val) => (
                <option key={val} value={val}>{val}× Leverage</option>
              ))}
            </select>
          </label>

          <label style={{ display: "flex", alignItems: "center", gap: 6, paddingBottom: 8, cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={form.use_bayesian}
              onChange={(e) => setForm({ ...form, use_bayesian: e.target.checked })}
            />
            Optuna Bayesian
          </label>

          <label style={{ display: "flex", alignItems: "center", gap: 6, paddingBottom: 8, cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={form.use_atr_sl}
              onChange={(e) => setForm({ ...form, use_atr_sl: e.target.checked })}
            />
            ATR-Adaptive SL
          </label>
        </div>

        <div style={{ display: "flex", justifyContent: "center", marginTop: 20 }}>
          <button
            type="submit"
            className="btn primary"
            style={{ minWidth: 220, padding: "12px 36px", fontSize: 14, letterSpacing: "0.03em" }}
          >
            Run Backtest
          </button>
        </div>
      </form>

      <details style={{ marginTop: 16 }}>
        <summary className="hint" style={{ cursor: "pointer", color: "var(--text-muted)" }}>
          ▼ Alternative: Push Code to Kaggle Runner
        </summary>
        <div style={{ marginTop: 8, padding: 12, background: "var(--bg)", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
          <p className="hint" style={{ margin: "0 0 10px 0" }}>
            Pushes configuration constants directly into the Kaggle runner script. Useful as a backup or for inspecting live terminal output.
          </p>
          <button type="button" className="btn" onClick={onSyncKaggle}>
            Sync to Kaggle
          </button>
          {kaggleUrl && (
            <a className="btn" href={kaggleUrl} target="_blank" rel="noopener noreferrer" style={{ marginLeft: 8 }}>
              Open in Kaggle →
            </a>
          )}
        </div>
      </details>

      {statusText && (
        <div className="mono" style={{ marginTop: 12, fontSize: 12, color: "var(--accent-secondary)" }}>
          {statusText}
        </div>
      )}
    </div>
  );
}