import React, { useEffect, useRef, useState, useMemo } from "react";
import Layout from "../components/Layout";
import BacktestForm from "../components/backtest/BacktestForm";
import BacktestConfirmModal from "../components/backtest/BacktestConfirmModal";
import RunDetailModal from "../components/backtest/RunDetailModal";
import RunsTable from "../components/backtest/RunsTable";
import { apiGet, apiPost } from "../lib/api";

export default function Backtest() {
  const [wfRuns, setWfRuns] = useState(null);
  const [optRuns, setOptRuns] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [bounds, setBounds] = useState(null);

  const [form, setForm] = useState({
    walk_forward_days: "15",
    start_date: "",
    end_date: "",
    bayesian_trials: "150",
    leverage: "10",
    use_bayesian: true,
    use_atr_sl: true,
  });

  const [statusText, setStatusText] = useState("");
  const [kaggleUrl, setKaggleUrl] = useState(null);
  const [modal, setModal] = useState(null);
  const [isConfirmOpen, setIsConfirmOpen] = useState(false);

  // 2026-11-12: drives the "Run Backtest" button's own animated state
  // (Queueing… / Running… / Backtest Completed / Backtest Failed) instead
  // of the old separate queueing/etc. text remarks below the form — see
  // BacktestForm's runPhase prop. activeRunId is the ONE run THIS submit
  // started (not just "any run active" — a background daily_optimizer
  // tick running at the same time must never make this button say
  // "Running"), tracked against wfRuns/optRuns on every poll tick below.
  const [runPhase, setRunPhase] = useState("idle"); // idle|queueing|running|completed|failed
  const [activeRunId, setActiveRunId] = useState(null);
  const resetTimerRef = useRef(null);

  const autoPollTimerRef = useRef(null);

  function scheduleRunPhaseReset(delayMs = 2500) {
    if (resetTimerRef.current) clearTimeout(resetTimerRef.current);
    resetTimerRef.current = setTimeout(() => {
      setRunPhase("idle");
      setActiveRunId(null);
    }, delayMs);
  }

  useEffect(() => () => {
    if (resetTimerRef.current) clearTimeout(resetTimerRef.current);
  }, []);

  // Tracks activeRunId's real status as loadRuns() polls, so the button
  // reflects the GitHub Actions job's actual queued → running →
  // completed/failed progression, not just "a request was sent."
  useEffect(() => {
    if (!activeRunId) return;
    const r = (wfRuns || []).find((x) => x.id === activeRunId) ||
              (optRuns || []).find((x) => x.id === activeRunId);
    if (!r) return; // not visible yet — stay on "queueing"
    if (r.status === "queued") setRunPhase("queueing");
    else if (r.status === "running") setRunPhase("running");
    else if (r.status === "completed") { setRunPhase("completed"); scheduleRunPhaseReset(); }
    else if (r.status === "failed") { setRunPhase("failed"); scheduleRunPhaseReset(); }
  }, [wfRuns, optRuns, activeRunId]);

  async function loadRuns() {
    try {
      const wf = await apiGet("/runs", { run_type: "walkforward" });
      setWfRuns(wf.runs);
      const opt = await apiGet("/runs", { run_type: "daily_optimizer" });
      setOptRuns(opt.runs);
      setLoadError(null);
    } catch (e) {
      setLoadError(e.message);
    }
  }

  useEffect(() => {
    loadRuns();
    apiGet("/backtests/bounds")
      .then(setBounds)
      .catch(() => {
        const now = new Date();
        const earliest = new Date(now.getTime() - 365 * 24 * 3600 * 1000);
        setBounds({
          earliest_date: earliest.toISOString().split("T")[0],
          latest_date: now.toISOString().split("T")[0],
        });
      });
  }, []);

  // Filter out zombie runs older than 45 minutes to prevent perpetual polling
  const isAnyRunActive = useMemo(() => {
    const nowMs = Date.now();
    const isRunFresh = (r) => {
      const ts = r.started_at || r.finished_at;
      if (!ts) return false;
      const ageMs = nowMs - new Date(ts).getTime();
      return ageMs < 45 * 60 * 1000;
    };

    return (
      (wfRuns && wfRuns.some((r) => (r.status === "queued" || r.status === "running") && isRunFresh(r))) ||
      (optRuns && optRuns.some((r) => (r.status === "queued" || r.status === "running") && isRunFresh(r)))
    );
  }, [wfRuns, optRuns]);

  // Background Auto-Refresh Polling Loop
  useEffect(() => {
    if (isAnyRunActive) {
      if (!autoPollTimerRef.current) {
        autoPollTimerRef.current = setInterval(() => {
          loadRuns();
        }, 8000);
      }
    } else {
      if (autoPollTimerRef.current) {
        clearInterval(autoPollTimerRef.current);
        autoPollTimerRef.current = null;
      }
    }

    return () => {
      if (autoPollTimerRef.current) {
        clearInterval(autoPollTimerRef.current);
        autoPollTimerRef.current = null;
      }
    };
  }, [isAnyRunActive]);

  function formBody() {
    const body = {};
    if (form.start_date) {
      body.start_date = form.start_date;
      if (form.end_date) body.end_date = form.end_date;
    } else if (form.walk_forward_days) {
      body.walk_forward_days = parseInt(form.walk_forward_days, 10);
    }
    body.bayesian_trials = parseInt(form.bayesian_trials, 10);
    body.leverage = parseFloat(form.leverage);
    body.use_bayesian = !!form.use_bayesian;
    body.use_atr_sl = !!form.use_atr_sl;
    return body;
  }

  async function handleConfirmedSubmit() {
    setRunPhase("queueing");
    setStatusText("");
    try {
      const result = await apiPost("/backtests", formBody());
      setActiveRunId(result.run_id);
      loadRuns();
    } catch (err) {
      setStatusText("Failed to queue backtest: " + err.message);
      setRunPhase("failed");
      scheduleRunPhaseReset();
    }
  }

  async function handleKaggleSync() {
    setKaggleUrl(null);
    setStatusText("Pushing parameters to Kaggle notebook…");
    try {
      const result = await apiPost("/backtests/sync-to-kaggle", formBody());
      setStatusText(`Run #${result.run_id} pushed.`);
      setKaggleUrl(result.kaggle_kernel_url);
      loadRuns();
    } catch (err) {
      setStatusText("Failed to sync to Kaggle: " + err.message);
    }
  }

  async function handleSelectRun(runId) {
    setModal({ runId, loading: true, run: null, params: {}, error: null, trades: null, curve: null });
    try {
      const run = await apiGet(`/runs/${runId}`);
      let params = {};
      try {
        params = typeof run.params === "string" ? JSON.parse(run.params) : (run.params || {});
      } catch {}

      let trades = null;
      try {
        const t = await apiGet(`/runs/${runId}/trades`);
        if (t.trades && t.trades.length > 0) trades = t.trades;
      } catch {}

      // 2026-11-02: the real per-bar capital_curve, when this run
      // persisted one — see pyquant_state.record_capital_curve() /
      // EquityCurveChart's "prefer the real curve" comment.
      let curve = null;
      try {
        const c = await apiGet(`/runs/${runId}/curve`);
        if (c.curve && c.curve.length > 1) curve = c.curve;
      } catch {}

      setModal({ runId, loading: false, run, params, error: null, trades, curve });
    } catch (e) {
      setModal({ runId, loading: false, run: null, params: {}, error: e.message, trades: null, curve: null });
    }
  }

  return (
    <Layout title="Backtest Studio" footer="PyQuant Backtest Studio — UKF Online Learning & Simulation Engine">
      {loadError && <div className="banner error">System Connection Error: {loadError}</div>}

      {/* Modular Form Component */}
      <BacktestForm
        form={form}
        setForm={setForm}
        bounds={bounds}
        onInitiateRun={() => setIsConfirmOpen(true)}
        onSyncKaggle={handleKaggleSync}
        kaggleUrl={kaggleUrl}
        statusText={statusText}
        runPhase={runPhase}
      />

      {/* Modular Runs Table Component */}
      <RunsTable
        wfRuns={wfRuns}
        optRuns={optRuns}
        onSelectRun={handleSelectRun}
        onRefresh={loadRuns}
        isAnyRunActive={isAnyRunActive}
      />

      {/* Pre-Flight Confirmation Modal */}
      <BacktestConfirmModal
        isOpen={isConfirmOpen}
        onClose={() => setIsConfirmOpen(false)}
        onConfirm={handleConfirmedSubmit}
        form={form}
        bounds={bounds}
      />

      {/* Modular Run Details & Trades Inspection Modal */}
      <RunDetailModal
        modal={modal}
        onClose={() => setModal(null)}
        bounds={bounds}
      />
    </Layout>
  );
}