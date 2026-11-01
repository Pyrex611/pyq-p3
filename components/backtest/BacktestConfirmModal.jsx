import React, { useMemo } from "react";

export default function BacktestConfirmModal({ isOpen, onClose, onConfirm, form, bounds }) {
  if (!isOpen) return null;

  const resolved = useMemo(() => {
    let mode = "Duration Days";
    let startStr = "";
    let endStr = "";
    let daysCount = form.walk_forward_days || 15;

    if (form.start_date) {
      mode = "Custom Date Range";
      startStr = form.start_date;
      endStr = form.end_date || bounds?.latest_date || new Date().toISOString().slice(0, 10);
      const st = new Date(startStr);
      const en = new Date(endStr);
      const diffDays = Math.max(1, Math.round((en - st) / (24 * 3600 * 1000)));
      daysCount = diffDays;
    } else {
      const en = new Date();
      const st = new Date(en.getTime() - daysCount * 24 * 3600 * 1000);
      startStr = st.toISOString().slice(0, 10);
      endStr = en.toISOString().slice(0, 10);
    }

    return { mode, startStr, endStr, daysCount };
  }, [form, bounds]);

  return (
    <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal-card" style={{ maxWidth: 540 }}>
        <button className="modal-close" onClick={onClose} aria-label="Close">&times;</button>
        <h3>Confirm Backtest Launch</h3>
        <div className="modal-sub">
          Review resolved execution parameters prior to dispatching to runner
        </div>

        <div style={{ background: "var(--bg)", padding: 14, borderRadius: 8, border: "1px solid var(--border)", marginBottom: 16 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px 14px", fontSize: 13 }}>
            <div>
              <span style={{ color: "var(--text-faint)", fontSize: 11, textTransform: "uppercase" }}>Selection Mode</span>
              <div style={{ fontWeight: 600, color: "var(--accent-secondary)" }}>{resolved.mode}</div>
            </div>
            <div>
              <span style={{ color: "var(--text-faint)", fontSize: 11, textTransform: "uppercase" }}>Estimated Window</span>
              <div style={{ fontWeight: 600, color: "#FFF" }}>{resolved.daysCount} Calendar Days</div>
            </div>
            <div>
              <span style={{ color: "var(--text-faint)", fontSize: 11, textTransform: "uppercase" }}>Start Date</span>
              <div className="mono">{resolved.startStr}</div>
            </div>
            <div>
              <span style={{ color: "var(--text-faint)", fontSize: 11, textTransform: "uppercase" }}>End Date</span>
              <div className="mono">{resolved.endStr}</div>
            </div>
            <div>
              <span style={{ color: "var(--text-faint)", fontSize: 11, textTransform: "uppercase" }}>Optuna Trials</span>
              <div className="mono">{form.bayesian_trials} Trials</div>
            </div>
            <div>
              <span style={{ color: "var(--text-faint)", fontSize: 11, textTransform: "uppercase" }}>Leverage</span>
              <div className="mono">{form.leverage}× Margin</div>
            </div>
          </div>
        </div>

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            style={{ padding: "8px 22px" }}
            onClick={() => {
              onClose();
              onConfirm();
            }}
          >
            Confirm & Dispatch
          </button>
        </div>
      </div>
    </div>
  );
}