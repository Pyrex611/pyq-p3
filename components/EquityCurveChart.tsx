import React, { useState, useMemo } from "react";
import { fmtMoney, fmtPct, fmtTime, pnlClass } from "../lib/api";

export default function EquityCurveChart({ trades, runMetrics = {}, initialCapital = 10.0 }) {
  const [hoverIndex, setHoverIndex] = useState(null);
  const [activeTab, setActiveTab] = useState("equity");

  const sortedTrades = useMemo(() => {
    return [...(trades || [])]
      .filter((t) => t.exit_ts || t.entry_ts)
      .sort((a, b) => new Date(a.exit_ts || a.entry_ts) - new Date(b.exit_ts || b.entry_ts));
  }, [trades]);

  const curveData = useMemo(() => {
    let currentCapital = initialCapital;
    let peakCapital = initialCapital;

    // Anchor Point 0 at start ($10.00, 0% PnL)
    const points = [
      {
        tradeIndex: 0,
        trade: { exit_leg: "INITIAL", pnl_pct: 0, profit: 0 },
        capital: initialCapital,
        cumPnl: 0,
        returnPct: 0,
        drawdownPct: 0,
        ts: sortedTrades[0]?.entry_ts || runMetrics["Start Date"] || new Date().toISOString(),
      },
    ];

    if (sortedTrades.length === 0) {
      const finalRetStr = String(runMetrics["Total Return (%)"] || "0").replace("%", "").trim();
      const finalRetPct = parseFloat(finalRetStr) || 0;
      const finalCapital = initialCapital * (1 + finalRetPct / 100);
      const maxDdStr = String(runMetrics["Max Drawdown (%)"] || "0").replace("%", "").trim();
      const maxDdPval = -(parseFloat(maxDdStr) || 0);

      points.push({
        tradeIndex: 1,
        trade: { exit_leg: "SUMMARY", pnl_pct: finalRetPct / 100, profit: finalCapital - initialCapital },
        capital: finalCapital,
        cumPnl: finalCapital - initialCapital,
        returnPct: finalRetPct,
        drawdownPct: maxDdPval,
        ts: runMetrics["End Date"] || new Date().toISOString(),
      });
      return points;
    }

    sortedTrades.forEach((t, idx) => {
      const pnl = t.profit ?? 0;
      currentCapital += pnl;
      if (currentCapital > peakCapital) peakCapital = currentCapital;
      const drawdownPct = peakCapital > 0 ? ((currentCapital - peakCapital) / peakCapital) * 100 : 0;
      const returnPct = ((currentCapital - initialCapital) / initialCapital) * 100;

      points.push({
        tradeIndex: idx + 1,
        trade: t,
        capital: currentCapital,
        cumPnl: currentCapital - initialCapital,
        returnPct,
        drawdownPct,
        ts: t.exit_ts || t.entry_ts,
      });
    });

    return points;
  }, [sortedTrades, initialCapital, runMetrics]);

  const W = 780;
  const H = 210;
  const PAD_X = 32;
  const PAD_Y = 18;

  const values = activeTab === "equity" ? curveData.map((d) => d.cumPnl) : curveData.map((d) => d.drawdownPct);
  const minVal = activeTab === "equity" ? Math.min(0, ...values) : Math.min(...values, -0.5);
  const maxVal = activeTab === "equity" ? Math.max(0, ...values) : 0;
  const span = Math.max(maxVal - minVal, 0.001);

  const toX = (idx) => PAD_X + (idx / Math.max(1, curveData.length - 1)) * (W - PAD_X * 2);
  const toY = (val) => PAD_Y + (1 - (val - minVal) / span) * (H - PAD_Y * 2);

  const zeroY = Math.min(H - PAD_Y, Math.max(PAD_Y, toY(0)));

  const points = curveData.map((d, i) => `${toX(i).toFixed(1)},${toY(values[i]).toFixed(1)}`).join(" ");
  const isPositive = curveData[curveData.length - 1].cumPnl >= 0;
  const strokeColor = activeTab === "equity" ? (isPositive ? "var(--green)" : "var(--red)") : "var(--red)";

  const areaPath = `M ${toX(0).toFixed(1)},${zeroY.toFixed(1)} ` +
    curveData.map((d, i) => `L ${toX(i).toFixed(1)},${toY(values[i]).toFixed(1)} `).join("") +
    `L ${toX(curveData.length - 1).toFixed(1)},${zeroY.toFixed(1)} Z`;

  const hoveredItem = hoverIndex !== null ? curveData[hoverIndex] : curveData[curveData.length - 1];

  return (
    <div className="modal-section" style={{ background: "#100F0F", padding: 16, borderRadius: 8, border: "1px solid var(--border)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div>
          <h4 style={{ margin: 0, fontSize: 12, textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--text)" }}>
            Simulation Trajectory
          </h4>
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
            {sortedTrades.length > 0 ? `${sortedTrades.length} Trade Points Plotted` : "Summary Performance Curve"}
          </span>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button
            type="button"
            className={`btn ${activeTab === "equity" ? "primary" : ""}`}
            style={{ padding: "4px 10px", fontSize: 11 }}
            onClick={() => setActiveTab("equity")}
          >
            Cumulative Return ($)
          </button>
          <button
            type="button"
            className={`btn ${activeTab === "drawdown" ? "primary" : ""}`}
            style={{ padding: "4px 10px", fontSize: 11 }}
            onClick={() => setActiveTab("drawdown")}
          >
            Underwater Drawdown (%)
          </button>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10, marginBottom: 12, padding: "8px 12px", background: "var(--bg-panel)", borderRadius: 6, border: "1px solid var(--border-soft)" }}>
        <div>
          <div style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase" }}>Point</div>
          <div className="mono" style={{ fontSize: 13, fontWeight: 600 }}>#{hoveredItem.tradeIndex} ({fmtTime(hoveredItem.ts)})</div>
        </div>
        <div>
          <div style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase" }}>Net Return</div>
          <div className={`mono ${pnlClass(hoveredItem.cumPnl)}`} style={{ fontSize: 13, fontWeight: 600 }}>
            {fmtMoney(hoveredItem.cumPnl)} ({hoveredItem.returnPct >= 0 ? "+" : ""}{hoveredItem.returnPct.toFixed(2)}%)
          </div>
        </div>
        <div>
          <div style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase" }}>Drawdown</div>
          <div className="mono" style={{ fontSize: 13, fontWeight: 600, color: hoveredItem.drawdownPct < 0 ? "var(--red)" : "var(--text-dim)" }}>
            {hoveredItem.drawdownPct.toFixed(2)}%
          </div>
        </div>
        <div>
          <div style={{ fontSize: 10, color: "var(--text-faint)", textTransform: "uppercase" }}>Trade PnL / Leg</div>
          <div className={`mono ${pnlClass(hoveredItem.trade.pnl_pct)}`} style={{ fontSize: 13, fontWeight: 600 }}>
            {hoveredItem.trade.exit_leg || "CLOSE"} ({fmtPct(hoveredItem.trade.pnl_pct)})
          </div>
        </div>
      </div>

      <div style={{ position: "relative" }}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height={H}
          style={{ display: "block", overflow: "visible" }}
          onMouseLeave={() => setHoverIndex(null)}
          onMouseMove={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            const relX = ((e.clientX - rect.left) / rect.width) * W;
            const boundedX = Math.max(PAD_X, Math.min(W - PAD_X, relX));
            const ratio = (boundedX - PAD_X) / (W - PAD_X * 2);
            const nearestIdx = Math.round(ratio * (curveData.length - 1));
            setHoverIndex(nearestIdx);
          }}
        >
          <line x1={PAD_X} y1={zeroY} x2={W - PAD_X} y2={zeroY} stroke="var(--border)" strokeWidth="1" strokeDasharray="4 3" />
          <path
            d={areaPath}
            fill={activeTab === "equity" ? (isPositive ? "rgba(63,185,80,0.12)" : "rgba(242,84,91,0.12)") : "rgba(242,84,91,0.15)"}
          />
          <polyline
            fill="none"
            stroke={strokeColor}
            strokeWidth="1.8"
            strokeLinejoin="round"
            strokeLinecap="round"
            points={points}
          />
          {curveData.map((d, i) => {
            const cx = toX(i);
            const cy = toY(values[i]);
            const isWin = (d.trade.profit ?? 0) > 0;
            return (
              <circle
                key={i}
                cx={cx}
                cy={cy}
                r={hoverIndex === i ? 5 : i === 0 ? 3 : 2}
                fill={i === 0 ? "var(--text-faint)" : isWin ? "var(--green)" : "var(--red)"}
                stroke="#100F0F"
                strokeWidth={1}
              />
            );
          })}
          {hoverIndex !== null && (
            <line
              x1={toX(hoverIndex)}
              y1={PAD_Y}
              x2={toX(hoverIndex)}
              y2={H - PAD_Y}
              stroke="var(--accent-secondary)"
              strokeWidth="1"
              strokeDasharray="2 2"
            />
          )}
        </svg>
      </div>
    </div>
  );
}