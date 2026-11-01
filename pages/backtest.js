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

  const autoPollTimerRef = useRef(null);

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
    setStatusText("Dispatching run to GitHub Actions runner…");
    try {
      const result = await apiPost("/backtests", formBody());
      setStatusText(`Run #${result.run_id} queued — auto-refresh active.`);
      loadRuns();
    } catch (err) {
      setStatusText("Failed to queue backtest: " + err.message);
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
    setModal({ runId, loading: true, run: null, params: {}, error: null, trades: null });
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

      setModal({ runId, loading: false, run, params, error: null, trades });
    } catch (e) {
      setModal({ runId, loading: false, run: null, params: {}, error: e.message, trades: null });
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
      />
    </Layout>
  );
}