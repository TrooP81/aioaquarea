"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { DataAge } from "./DataAge";
import { LAYER_LABELS, MODEL_REPORTING_LABELS, reasonLabel, seasonalReasonLabel } from "@/lib/constants";
import type { ControlState, ModelComponent, ModelLifecycleStatus, ModelLifecycleStatuses, ModelReportingState, ReadinessComponent, SeasonalReadiness, SpaceHeatingGate } from "@/lib/api-types";
import { OptimizerDiagnostics } from "./OptimizerDiagnostics";
import { WhyItDecides } from "./WhyItDecides";
import { ManualCOPMonitoring } from "./ManualCOPMonitoring";

interface ModelInfo {
  trained: boolean;
  status?: ModelReportingState;
  last_trained: string | null;
  samples?: number;
  source_records?: number | null;
  metrics?: { mae?: number; cv_std?: number; samples?: number; baseline_mae?: number; validation_method?: string };
  unavailable_reason?: "retired" | "not_found" | "integrity_check_failed" | "incompatible_artifact" | "load_failed" | null;
  data_quality?: DemandDataQuality;
}

interface DemandDataQuality {
  raw_records: number;
  intervals: number;
  usable_samples: number | null;
  minimum_samples: number;
  rejected_nonpositive: number;
  rejected_rate_bounds: number;
  weather_matches: number;
  remaining_samples?: number | null;
  heating_activity_ratio?: number;
  training_blocker?: string | null;
  seasonal_guidance?: string;
  ready_to_train: boolean;
}

interface ModelTrainResult {
  error?: string;
  samples?: number;
  version?: string;
  status?: string;
  request_id?: string;
  maintenance_enabled?: boolean;
}

interface ThermalInfo {
  calibrated: boolean;
  tank_heating_rate: number | null;
  confidence: string;
  indoor_heating_confidence: string;
  indoor_heating_samples: number | null;
  calibration_status?: Record<string, string>;
  last_calibrated: string | null;
}

interface ComfortInfo {
  trained: boolean;
  control_ready: boolean;
  control_readiness: { reason?: string } | null;
  last_trained: string | null;
  training_samples: number;
  metrics: Record<string, unknown> | null;
  training_notice?: string | null;
  control_margin_c?: number;
  passive_forecast?: Record<string, { ready?: boolean; mae?: number; horizon_minutes?: number }>;
  lifecycle?: ModelLifecycleStatus;
}

interface IndoorTempLatest {
  avg_temperature: number | null;
  latest_reading: string | null;
  sensor_count: number;
  last_fresh_reading: string | null;
  sample_count?: number;
  confidence?: string;
  reason?: string | null;
  reference_sensor_id?: string | null;
  reference_sensor_label?: string | null;
  reference_room?: string | null;
  spread_c?: number | null;
  sensors?: { device_id: string; device_label?: string | null; room?: string | null; temperature: number; timestamp: string }[];
}

interface LastPlanInfo {
  version: string;
  engine: string;
  fell_back: boolean;
  created_at: string | null;
}

interface OptimizerStatusData {
  configured_layer: string;
  active_layer: string;
  fallback_layer: string;
  last_plan: LastPlanInfo | null;
  data_freshness?: {
    latest_device_status: string | null;
    age_seconds: number | null;
    stale_after_seconds: number;
    fresh: boolean;
  };
  planning_data_quality?: {
    control_allowed: boolean;
    reasons: string[];
    effective_horizon_hours?: number;
    price_horizon_limited?: boolean;
    reoptimization_when_prices_extend?: boolean;
    price?: { contiguous_hours?: number; complete_horizon?: boolean; fresh?: boolean; age_seconds?: number | null; next_publication_check_seconds?: number | null };
  };
  comfort_controllability?: {
    status: "not_heatpump_controllable" | "awaiting_sensor" | "within_band" | "heat_curve_controllable";
    message: string;
    outdoor_temp_c?: number | null;
    cutoff_c?: number | null;
  };
  seasonal_calibration?: {
    enabled: boolean | null;
    observe_only_active: boolean | null;
    heating_season_detected: boolean | null;
    reason: string;
    average_outdoor_c: number | null;
    next_step?: string;
    auto_train?: boolean | null;
    auto_exit?: boolean;
    readiness?: SeasonalReadiness;
    demand?: { usable_samples?: number | null; minimum_samples?: number; remaining_samples?: number | null; trained?: boolean };
    indoor_heating?: { samples?: number | null; minimum_samples?: number; remaining_samples?: number | null };
  };
  decision_readiness?: { state: "ready" | "collecting" | "fallback"; title: string; detail: string };
  cop_model: ModelInfo;
  demand_model: ModelInfo;
  thermal_model: ThermalInfo;
  model_lifecycle?: ModelLifecycleStatuses;
}

interface ForecastScoreBucket {
  samples: number;
  mae: number | null;
  bias: number | null;
  p90_abs_error: number | null;
}

interface ForecastEvidence {
  plans_scored: number;
  overall: ForecastScoreBucket;
  horizons: Array<ForecastScoreBucket & { hours: number }>;
  regimes: Record<string, ForecastScoreBucket>;
  horizon_quality?: Record<string, { status: "passed" | "failed" | "observing"; reason?: string }>;
  coverage?: { observed_regimes?: string[]; unobserved_regimes?: string[]; minimum_regime_samples?: number };
  regime_quality?: Record<string, { status: "passed" | "failed" | "unobserved"; reason?: string }>;
  bias_correction?: { overall_c?: number; maximum_abs_c?: number };
  prediction_interval?: {
    coverage?: number;
    overall?: {
      status?: "calibrated" | "estimated";
      lower_offset_c?: number;
      upper_offset_c?: number;
      samples?: number;
    };
  };
  quality_gate: {
    status: "passed" | "failed" | "observing";
    control_allowed: boolean;
    reason: string;
  };
}

interface ForecastScorecard extends ForecastEvidence {
  fallback?: ForecastEvidence;
  passive_weather_model?: ForecastEvidence;
  all_forecasts?: ForecastEvidence;
  exclusions?: Record<string, number>;
  note: string;
}

interface SensorDiagnostics {
  mode: "shadow";
  controls_unchanged: boolean;
  sensor_count: number;
  room_spread_c?: number | null;
  summary: string;
  sensors: Array<{
    device_id: string;
    label?: string | null;
    room?: string | null;
    state: string;
    fresh_samples: number;
    age_seconds: number;
    median_offset_from_rooms_c?: number | null;
    is_reference: boolean;
  }>;
}

type ModelState = ModelReportingState;

interface ModelCard {
  component: ModelComponent;
  lifecycle?: ModelLifecycleStatus;
  label: string;
  plain: string;
  state: ModelState;
  lastTrained: string | null;
  detail: string;
  unavailableReason?: string;
  nextStep: string;
  capabilities?: Array<{ label: string; state: "ready" | "learning" | "waiting" | "unknown" }>;
}

interface TrainMessage {
  text: string;
  tone: "success" | "info" | "error";
}

function StatusDot({ state }: { state: ModelState }) {
  return (
    <span className={`status-dot ${state === "qualified-for-reporting" ? "status-dot--ok" : state === "candidate" ? "status-dot--observing" : ""}`} />
  );
}

function modelStateLabel(state: ModelState): string {
  return MODEL_REPORTING_LABELS[state];
}

function formatDate(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "unknown";
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function readinessStepLabel(step: string): string {
  const labels: Record<string, string> = {
    await_fit_or_validation: "Awaiting fit or validation.",
    await_successful_demand_training: "Awaiting demand fit and validation.",
    await_indoor_heating_fit: "Awaiting indoor-heating fit and validation.",
    await_prospective_reporting_qualification: "Awaiting prospective reporting qualification.",
    collect_space_heating_evidence: "Collecting eligible heating evidence.",
    collect_indoor_heating_evidence: "Collecting eligible indoor-heating evidence.",
    wait_for_heating_season: "Waiting for heating-season observations.",
    enable_observe_only_campaign: "Observe-only campaign is not enabled.",
    ready_for_reporting: "Qualified for reporting; observation is unchanged.",
    readiness_unavailable: "Readiness unavailable.",
  };
  return labels[step] ?? reasonLabel(step);
}

function readinessReasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    running_request_proof_unavailable: "Current maintenance activity is not proven.",
    no_running_request: "No running maintenance request.",
    lifecycle_not_initialized: "Maintenance lifecycle not initialized.",
    insufficient_train: "Training partition below minimum.",
    insufficient_holdout: "Holdout partition below minimum.",
    no_persisted_fit: "No persisted fit.",
    snapshot_materialization_unavailable: "Snapshot unavailable.",
    readiness_input_budget_exceeded: "Snapshot exceeds the input budget.",
    readiness_row_limit_exceeded: "Source history exceeds the row limit.",
    nonfinite_training_inputs: "Training inputs contain invalid values.",
    source_not_assessed: "Source inputs not assessed.",
    reference_sensor_not_selected: "Reference sensor is not selected.",
    seasonal_reporting_unavailable: "Seasonal evidence unavailable.",
    awaiting_explicit_seasonal_cycle: "Awaiting the explicit seasonal cycle.",
  };
  return labels[reason] ?? reasonLabel(reason);
}

function ReadinessDetails({ name, label, stage }: { name: string; label: string; stage: ReadinessComponent }) {
  const collection = stage.collection;
  const progress = collection.train != null && collection.minimum_train != null && collection.minimum_train > 0
    ? Math.min(100, Math.round(100 * collection.train / collection.minimum_train)) : null;
  const fit = stage.fit;
  const reporting = stage.reporting;
  return (
    <div data-testid={`readiness-${name}`} style={{ marginTop: "0.75rem", minWidth: 0, overflowWrap: "anywhere" }}>
      <strong>{label}</strong>
      {name === "demand" && <div>Demand evidence: {collection.total ?? "Unknown"}/{collection.minimum_train ?? "Unknown"} total milestone</div>}
      <div>Total: {collection.total ?? "Unknown"}; Train: {collection.train ?? "Unknown"}/{collection.minimum_train ?? "Unknown"}; Holdout: {collection.holdout ?? "Unknown"}/{collection.minimum_holdout ?? "Unknown"}</div>
      {progress != null && <progress value={progress} max={100} aria-label={`${label} training collection`} style={{ width: "100%", display: "block" }} />}
      {collection.reason && <div>Collection: {readinessReasonLabel(collection.reason)}</div>}
      <div>Fit: {fit.status === "validated" ? "Validated" : fit.status === "awaiting_fit" ? "Not yet fitted" : fit.status ? reasonLabel(fit.status) : "Unknown"}{fit.reason ? `; ${readinessReasonLabel(fit.reason)}` : ""}</div>
      <div>Fit cutoff: {fit.data_cutoff ? formatDate(fit.data_cutoff) : "Not assessed"}; Reference: {fit.artifact_hash?.slice(0, 12) ?? fit.request_id ?? "Not assessed"}</div>
      <div>Native outcomes: {reporting.counts?.pairs ?? "Not assessed"}; Missing: {reporting.missing_counts?.paired_outcomes ?? "Unknown"}; Pending: {reporting.pending_outcomes ?? "Unknown"}</div>
      <div>Device-days missing: {reporting.missing_counts?.device_day_clusters ?? "Unknown"}; Cohorts missing: {reporting.missing_counts?.issue_cohorts ?? "Unknown"}; Outdoor bins missing: {reporting.missing_diversity?.populated_outdoor_bins ?? "Unknown"}</div>
      <div>Checkpoint: {reporting.next_decision_checkpoint ? formatDate(reporting.next_decision_checkpoint.at) : "Not assessed"}; Qualification expiry: {reporting.qualification_expires_at ? formatDate(reporting.qualification_expires_at) : "Not assessed"}</div>
      <div>Next: {readinessStepLabel(stage.next_step)}</div>
    </div>
  );
}

function LifecycleDetails({ component, status, maintenance }: { component: ModelComponent; status?: ModelLifecycleStatus; maintenance?: SeasonalReadiness["maintenance"] }) {
  const missing = status?.missing_counts;
  const diversity = status?.missing_diversity;
  const checkpoint = status?.next_decision_checkpoint;
  const exclusions = Object.entries(status?.exclusion_counts ?? {});
  const horizonGaps = Object.entries(diversity?.pairs_per_horizon ?? {});
  const binGaps = Object.entries(diversity?.pairs_per_outdoor_bin ?? {});
  const available = Boolean(status && status.status !== "unavailable");
  const maintenanceEnabled = maintenance ? maintenance.enabled : status?.maintenance_enabled;
  const nextMaintenance = maintenance ? maintenance.next_due_at : status?.next_cadence_at;
  return (
    <div className="model-card-details" data-testid={`lifecycle-${component}`} data-reporting-state={status?.status ?? "unavailable"} style={{ overflowWrap: "anywhere" }}>
      <div>Incumbent: {modelStateLabel(status?.incumbent_status ?? "unavailable")}</div>
      <div>Candidate: {status?.candidate_status ? modelStateLabel(status.candidate_status) : available ? "None recorded" : "Unavailable"}</div>
      {status?.candidate_progress && <div>Candidate last valid outcome: {status.candidate_progress.last_valid_outcome_at ? formatDate(status.candidate_progress.last_valid_outcome_at) : "None recorded"}</div>}
      <div>Evidence: {status?.evidence_for ?? "unavailable"}</div>
      <div>Paired outcomes: {status?.counts?.pairs ?? "unavailable"}; missing: {missing?.paired_outcomes ?? "unavailable"}</div>
      <div>Device-day clusters: {status?.counts?.device_day_clusters ?? "unavailable"}; missing: {missing?.device_day_clusters ?? "unavailable"}</div>
      <div>Issue cohorts: {status?.counts?.issue_cohorts ?? "unavailable"}; missing: {missing?.issue_cohorts ?? "unavailable"}</div>
      <div>Elapsed days missing: {missing?.elapsed_days != null ? missing.elapsed_days.toFixed(1) : "unavailable"}</div>
      <div>Outdoor bins missing: {diversity?.populated_outdoor_bins ?? "unavailable"}</div>
      <div>Horizon pairs missing: {horizonGaps.length ? horizonGaps.map(([horizon, count]) => `${horizon} min: ${count}`).join("; ") : "unavailable"}</div>
      {binGaps.length > 0 && <div>Outdoor-bin pairs missing: {binGaps.map(([bin, count]) => `${bin} C: ${count}`).join("; ")}</div>}
      <div>Exclusions: {exclusions.length ? exclusions.map(([reason, count]) => `${reason.replaceAll("_", " ")}: ${count}`).join("; ") : status?.counts ? "0 recorded" : "unavailable"}</div>
      <div>Pending outcomes: {status?.pending_outcomes ?? "unavailable"}</div>
      <div>Next maintenance: {maintenanceEnabled === false ? "Disabled" : nextMaintenance ? formatDate(nextMaintenance) : maintenanceEnabled == null ? "Unknown" : "Unavailable"}</div>
      {status?.cadence_hours != null && <div>Minimum attempt cadence: {status.cadence_hours} hours</div>}
      <div>Last valid outcome: {status?.last_valid_outcome_at ? formatDate(status.last_valid_outcome_at) : available ? "None recorded" : "Unavailable"}</div>
      <div>Qualification expires: {status?.qualification_expires_at ? formatDate(status.qualification_expires_at) : available ? "None recorded" : "Unavailable"}</div>
      <div>Next decision checkpoint: {checkpoint ? `#${checkpoint.index + 1} at ${formatDate(checkpoint.at)}${checkpoint.due ? " (due; awaiting maintenance)" : ""}` : available ? "None scheduled" : "Unavailable"}</div>
      <div>Ordinary writes are prohibited in this build. Optimization requires a new reviewed, approved build.</div>
    </div>
  );
}

function layerBadgeClass(layer: string): string {
  if (layer.includes("ml")) return "opt-layer-badge opt-layer-badge--ml";
  if (layer.includes("milp")) return "opt-layer-badge opt-layer-badge--milp";
  return "opt-layer-badge";
}

function modelUnavailableReason(reason: ModelInfo["unavailable_reason"]): string | undefined {
  if (reason === "integrity_check_failed") return "Saved model failed its integrity check.";
  if (reason === "incompatible_artifact") return "Saved model is from an older format - retrain to update.";
  return undefined;
}

export function OptimizerStatus({ controlState, spaceHeatingGate }: { controlState: ControlState | null; spaceHeatingGate: SpaceHeatingGate | null }) {
  const [status, setStatus] = useState<OptimizerStatusData | null>(null);
  const [comfort, setComfort] = useState<ComfortInfo | null>(null);
  const [indoorTemp, setIndoorTemp] = useState<IndoorTempLatest | null>(null);
  const [forecastScorecard, setForecastScorecard] = useState<ForecastScorecard | null>(null);
  const [sensorDiagnostics, setSensorDiagnostics] = useState<SensorDiagnostics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [training, setTraining] = useState<Record<string, boolean>>({});
  const [trainMsg, setTrainMsg] = useState<TrainMessage | null>(null);
  const decisionFocusOrigin = useRef<Element | null>(null);

  const refresh = async (focusDecisionSummary = false) => {
    const focusOrigin = focusDecisionSummary ? document.activeElement : null;
    setIsRefreshing(true);
    try {
      const required = await fetch("/api/optimizer/status");
      if (!required.ok) throw new Error("Failed to load optimizer status");
      const [opt, cmf, temp, scorecard, diagnostics] = await Promise.all([
        required.json(),
        fetch("/api/comfort-model/status").then((response) => response.ok ? response.json() : null).catch(() => null),
        fetch("/api/indoor-temp/latest").then((response) => response.ok ? response.json() : null).catch(() => null),
        fetch("/api/thermal/forecast-scorecard").then((response) => response.ok ? response.json() : null).catch(() => null),
        fetch("/api/sensors/diagnostics").then((response) => response.ok ? response.json() : null).catch(() => null),
      ]);
      decisionFocusOrigin.current = focusOrigin;
      setStatus(opt);
      setComfort(cmf);
      setIndoorTemp(temp);
      setForecastScorecard(scorecard);
      setSensorDiagnostics(diagnostics);
      setError(null);
    } catch {
      decisionFocusOrigin.current = null;
      setError("Failed to load optimizer status");
    } finally {
      setIsRefreshing(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  useLayoutEffect(() => {
    const focusOrigin = decisionFocusOrigin.current;
    if (!focusOrigin || error || isRefreshing || !status) return;
    decisionFocusOrigin.current = null;
    if (document.activeElement === focusOrigin
      || (document.activeElement === document.body && !focusOrigin.isConnected)) {
      document.getElementById("why-it-decides-heading")?.focus();
    }
  }, [error, isRefreshing, status]);

  const trainMl = async () => {
    setTraining((p) => ({ ...p, ml: true }));
    setTrainMsg(null);
    try {
      const res = await fetch("/api/ml/train", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Training failed");
      const result: ModelTrainResult | undefined = data.demand;
      if (result?.status === "queued" && result.request_id) {
        setTrainMsg({ text: `Demand maintenance queued (${result.request_id}).${result.maintenance_enabled === false ? " Maintenance disabled." : ""}`, tone: "info" });
      } else if (result?.error) {
        setTrainMsg({ text: `Demand: ${result.error}${result.samples != null ? ` (${result.samples} samples)` : ""}`, tone: "error" });
      } else if (result?.version) {
        setTrainMsg({ text: "Demand model trained.", tone: "success" });
      } else {
        setTrainMsg({ text: `Demand training did not publish a model${result?.status ? ` (${result.status})` : ""}.`, tone: "error" });
      }
      await refresh();
    } catch (e) {
      setTrainMsg({ text: e instanceof Error ? e.message : "Training failed", tone: "error" });
    } finally {
      setTraining((p) => ({ ...p, ml: false }));
    }
  };

  const trainComfort = async () => {
    setTraining((p) => ({ ...p, comfort: true }));
    setTrainMsg(null);
    try {
      const res = await fetch("/api/comfort-model/train", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Training failed");
      if (data.error) throw new Error(data.error);
      if (data.status === "queued" && typeof data.request_id === "string") {
        setTrainMsg({ text: `Comfort maintenance queued (${data.request_id}).${data.maintenance_enabled === false ? " Maintenance disabled." : ""}`, tone: "info" });
        await refresh();
        return;
      }
      if (data.status === "training_in_progress") {
        setTrainMsg({ text: "Training already in progress", tone: "info" });
        await refresh();
        return;
      }
      if (data.status === "training_skipped") {
        setTrainMsg({ text: `Training skipped: ${data.reason ?? "unavailable"}`, tone: "error" });
        await refresh();
        return;
      }
      if (data.training_notice === "training_lock_finalize_recovered") {
        setTrainMsg({ text: "Training completed: lock finalization recovered", tone: "success" });
        await refresh();
        return;
      }
      const reason = typeof data.control_readiness?.reason === "string"
        ? reasonLabel(data.control_readiness.reason)
        : "validation is still pending";
      setTrainMsg({
        text: `Comfort model trained for reporting only (${reason}). Ordinary writes remain prohibited in this build.`,
        tone: "info",
      });
      await refresh();
    } catch (e) {
      setTrainMsg({ text: e instanceof Error ? e.message : "Training failed", tone: "error" });
    } finally {
      setTraining((p) => ({ ...p, comfort: false }));
    }
  };

  const calibrateThermal = async () => {
    setTraining((p) => ({ ...p, thermal: true }));
    setTrainMsg(null);
    try {
      const res = await fetch("/api/thermal/calibrate", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Calibration failed");
      if (data.status === "queued" && typeof data.request_id === "string") {
        setTrainMsg({ text: `Thermal maintenance queued (${data.request_id}).${data.maintenance_enabled === false ? " Maintenance disabled." : ""}`, tone: "info" });
      } else {
        setTrainMsg({ text: "Thermal model calibrated", tone: "success" });
      }
      await refresh();
    } catch (e) {
      setTrainMsg({ text: e instanceof Error ? e.message : "Calibration failed", tone: "error" });
    } finally {
      setTraining((p) => ({ ...p, thermal: false }));
    }
  };

  if (error) {
    return (
      <div className="plan-section">
        <h2 className="chart-title">How the optimizer is deciding</h2>
        <p className="text-danger">{error}</p>
        <button type="button" className="btn btn-sm" onClick={() => refresh(true)} disabled={isRefreshing} aria-busy={isRefreshing}>
          {isRefreshing ? "Retrying..." : "Retry"}
        </button>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="plan-section">
        <h2 className="chart-title">How the optimizer is deciding</h2>
        <div className="plan-loading">
          <div className="plan-loading-skeleton" />
          <div className="plan-loading-skeleton" style={{ width: "60%" }} />
        </div>
      </div>
    );
  }

  const demandQuality = status.demand_model.data_quality;
  const readiness = status.seasonal_calibration?.readiness;
  const demandStage = readiness?.components.demand;
  const indoorStage = readiness?.components.indoor_heating;
  const lifecycleStatuses = status.model_lifecycle ?? controlState?.model_lifecycle ?? {};
  const dataFreshness = status.data_freshness ?? {
    latest_device_status: null,
    age_seconds: null,
    stale_after_seconds: 0,
    fresh: false,
  };
  const demandDetail = demandQuality
    ? `${status.demand_model.source_records ?? "Unknown"} energy readings${status.demand_model.metrics?.mae != null ? ` · validation MAE ${status.demand_model.metrics.mae.toFixed(3)} kW` : ""} · ${demandQuality.usable_samples ?? "Unknown"}/${demandQuality.minimum_samples} usable intervals${demandQuality.remaining_samples ? ` · ${demandQuality.remaining_samples} still needed` : ""}${demandQuality.training_blocker === "waiting_for_space_heating_season" ? " · waiting for heating season" : ""}${demandQuality.rejected_rate_bounds > 0 ? ` · ${demandQuality.rejected_rate_bounds} rejected as implausible rate` : ""}${demandQuality.weather_matches > 0 ? ` · weather matched ${demandQuality.weather_matches}` : ""}`
    : `${status.demand_model.source_records ?? "Unknown"} energy readings${status.demand_model.metrics?.mae != null ? ` · validation MAE ${status.demand_model.metrics.mae.toFixed(3)} kW` : ""}`;

  const models: ModelCard[] = [
    {
      component: "demand",
      lifecycle: lifecycleStatuses.demand,
      label: "Demand Model",
      plain: demandQuality?.seasonal_guidance ?? "Forecasts electrical space-heating demand from weather and usage patterns",
      state: lifecycleStatuses.demand?.status ?? status.demand_model.status ?? "unavailable",
      lastTrained: status.demand_model.last_trained,
      detail: demandDetail,
      unavailableReason: modelUnavailableReason(status.demand_model.unavailable_reason),
      nextStep: demandStage ? readinessStepLabel(demandStage.next_step) : lifecycleStatuses.demand?.status === "qualified-for-reporting"
        ? "Reporting qualification is current; observation is unchanged."
        : status.demand_model.trained
          ? "Prospective reporting qualification is pending."
          : demandQuality?.training_blocker === "waiting_for_space_heating_season"
            ? "Wait for heating weather; manual retraining cannot create the missing evidence."
            : `${demandQuality?.remaining_samples ?? "Unknown"} additional usable heating interval(s) are needed.`,
    },
    {
      component: "thermal",
      lifecycle: lifecycleStatuses.thermal,
      label: "Thermal Model",
      plain: "Learns how fast your tank and rooms heat up",
      state: lifecycleStatuses.thermal?.status ?? "unavailable",
      lastTrained: status.thermal_model.last_calibrated,
      detail: `Artifact diagnostics · Tank: ${status.thermal_model.tank_heating_rate ?? "unavailable"} °C/h · ${status.thermal_model.confidence ?? "unavailable"} · Indoor fit: ${status.thermal_model.indoor_heating_confidence ?? "unavailable"} (${status.thermal_model.indoor_heating_samples ?? "unavailable"} artifact samples)${status.thermal_model.calibration_status?.zone_cooling ? ` · Zone cooling: ${reasonLabel(status.thermal_model.calibration_status.zone_cooling)}` : ""}`,
      nextStep: indoorStage ? readinessStepLabel(indoorStage.next_step) : lifecycleStatuses.thermal?.status === "qualified-for-reporting"
        ? "Qualified only for the declared reporting horizons and thermal components."
        : status.thermal_model.indoor_heating_confidence === "learned"
          ? "A learned fit does not confer prospective reporting qualification."
          : "Tank calibration is available, but indoor heating still uses safe defaults until confirmed heating samples exist.",
      capabilities: [
        { label: "Tank response", state: lifecycleStatuses.thermal?.status === "qualified-for-reporting" && lifecycleStatuses.thermal.qualification_scope?.capabilities?.includes("tank_heating") ? "ready" : status.thermal_model.calibrated ? "learning" : "waiting" },
        { label: "Indoor heating", state: lifecycleStatuses.thermal?.status === "qualified-for-reporting" && lifecycleStatuses.thermal.qualification_scope?.capabilities?.includes("indoor_heating") ? "ready" : indoorStage ? indoorStage.collection.total == null ? "unknown" : indoorStage.collection.total > 0 ? "learning" : "waiting" : status.thermal_model.indoor_heating_samples != null && status.thermal_model.indoor_heating_samples > 0 ? "learning" : "unknown" },
      ],
    },
  ];

  if (comfort || lifecycleStatuses.comfort) {
    const metrics = comfort?.metrics ?? {};
    const mae = typeof metrics.mae === "number" ? metrics.mae.toFixed(3) : null;
    const r2 = typeof metrics.r2 === "number" ? metrics.r2.toFixed(3) : null;
    const persistenceMae = typeof metrics.baseline_mae === "number" ? metrics.baseline_mae.toFixed(3) : null;
    const activeRows = typeof metrics.active_heating_rows === "number" ? metrics.active_heating_rows : null;
    const horizon = typeof metrics.training_horizon_minutes === "number" ? metrics.training_horizon_minutes : null;
    const sensorCount = typeof metrics.source_sensor_count === "number" ? metrics.source_sensor_count : null;
    const sensorStrategy = typeof metrics.sensor_strategy === "string" ? reasonLabel(metrics.sensor_strategy) : null;
    const metricsStr = [
      mae && `MAE ${mae}°C`,
      r2 && `R² ${r2}`,
      persistenceMae && `persistence MAE ${persistenceMae}°C`,
      activeRows !== null && `${activeRows} confirmed heating samples`,
      horizon && `${horizon}-min horizon`,
      sensorStrategy && `${sensorStrategy}${sensorCount !== null ? ` (${sensorCount} sensor${sensorCount === 1 ? "" : "s"})` : ""}`,
    ].filter(Boolean).join(" · ");
    models.push({
      component: "comfort",
      lifecycle: lifecycleStatuses.comfort ?? comfort?.lifecycle,
      label: "Comfort Model",
      plain: comfort?.trained
        ? "Trained for reporting only"
        : comfort?.training_notice ?? "Collecting data before indoor-temperature training can begin",
      state: lifecycleStatuses.comfort?.status ?? comfort?.lifecycle?.status ?? "unavailable",
      lastTrained: comfort?.last_trained ?? lifecycleStatuses.comfort?.last_trained ?? null,
      detail: `${comfort?.training_samples ?? lifecycleStatuses.comfort?.samples ?? "Unknown"} samples${metricsStr ? ` · ${metricsStr}` : ""}${comfort?.control_margin_c ? ` · ${comfort.control_margin_c.toFixed(2)}°C planning reserve` : ""}${comfort?.control_readiness?.reason ? ` · ${reasonLabel(comfort.control_readiness.reason)}` : ""}`,
      nextStep: lifecycleStatuses.comfort?.status === "qualified-for-reporting"
        ? "Reporting qualification is current; observation is unchanged."
        : comfort?.training_notice ?? "Prospective reporting qualification is pending.",
    });
  }

  const activeLayerLabel = LAYER_LABELS[status.active_layer] || status.active_layer;
  const demandTrainingBlocked = !demandQuality?.ready_to_train
    || demandQuality.usable_samples == null || demandQuality.usable_samples < demandQuality.minimum_samples;
  const comfortActiveHeatingRows = typeof comfort?.metrics?.active_heating_rows === "number"
    ? comfort.metrics.active_heating_rows
    : 0;
  const comfortTrainingBlocked = Boolean(comfort?.trained && comfortActiveHeatingRows === 0);
  const forecastFamilies = forecastScorecard ? [
    {
      label: "Comfort model · forecast evidence",
      detail: "Forecast evidence is reporting-only in this build.",
      evidence: forecastScorecard as ForecastEvidence,
    },
    {
      label: "Passive weather model",
      detail: "Direct no-space-heat forecast for warm periods; it does not approve heating control.",
      evidence: forecastScorecard.passive_weather_model,
    },
    {
      label: "Rules thermal fallback",
      detail: "Rule-based forecast quality is measured separately from ML validation.",
      evidence: forecastScorecard.fallback,
    },
  ] : [];
  const layerExplanation = status.configured_layer === "auto"
    ? "Automatic mode uses rules while COP is retired."
    : `Fixed setting — using ${activeLayerLabel}.`;
  const limitations = [
    "Ordinary writes are prohibited in this build; optimization requires a new reviewed, approved build.",
    !dataFreshness.fresh ? "Live pump status is stale; restore preconditions may be unavailable." : null,
    status.planning_data_quality && !status.planning_data_quality.control_allowed ? "Planning data currently prevents new plans." : null,
    status.comfort_controllability?.message,
    status.seasonal_calibration?.observe_only_active ? "Seasonal observation is active, so device commands are paused while evidence is collected." : null,
  ].filter((value): value is string => Boolean(value));

  return (
    <div className="plan-section">
      <WhyItDecides controlState={controlState} activeLayer={activeLayerLabel} fallbackActive={Boolean(status.last_plan?.fell_back)} limitations={limitations} models={models} />
      <OptimizerDiagnostics>

        {/* Active layer badge */}
        <section>
          <h3>Decision engine details</h3>
          <div className="opt-layer-row">
            <span className="optimizer-status-label text-muted">Decision engine</span>
            <span className={layerBadgeClass(status.active_layer)}>
              {activeLayerLabel}
            </span>
            <span className="text-muted text-xs">
              {layerExplanation}
            </span>
          </div>
          {status.last_plan && (
            <div className="opt-layer-row">
              <span className="text-muted text-sm">Last plan engine</span>
              <span className={layerBadgeClass(status.last_plan.version)}>
                {status.last_plan.engine.toUpperCase()}
              </span>
              {status.last_plan.fell_back && (
                <span className="text-warning text-xs">
                  MILP unavailable — fell back to rules
                </span>
              )}
            </div>
          )}
          <p className={dataFreshness.fresh ? "text-muted text-xs" : "text-warning text-sm"}>
            Live pump status: <DataAge timestamp={dataFreshness.latest_device_status} stale={!dataFreshness.fresh} />
            {!dataFreshness.fresh && " - restore preconditions may be unavailable."}
          </p>
          {status.decision_readiness && (
            <div className={status.decision_readiness.state === "ready" ? "indoor-temp-card" : "indoor-temp-card text-muted"} style={{ margin: "0.75rem 0" }}>
              <strong>{status.decision_readiness.title}</strong>
              <div className="text-sm">{status.decision_readiness.detail}</div>
            </div>
          )}
          {status.planning_data_quality && !status.planning_data_quality.control_allowed && (
            <p className="text-warning text-sm">
              New plans are paused: {status.planning_data_quality.reasons.join(" ")}
            </p>
          )}
          {status.planning_data_quality?.price_horizon_limited && (
            <p className="text-muted text-xs">
              Price publication: {status.planning_data_quality.price?.contiguous_hours ?? status.planning_data_quality.effective_horizon_hours ?? "limited"}h published and {status.planning_data_quality.price?.fresh ? "fresh" : "awaiting refresh"}. The plan is limited to published prices and will refresh when tomorrow&apos;s prices arrive; next check within {Math.round((status.planning_data_quality.price?.next_publication_check_seconds ?? 900) / 60)} min.
            </p>
          )}
          {status.comfort_controllability && (
            <p className={status.comfort_controllability.status === "not_heatpump_controllable" ? "text-warning text-sm" : "text-muted text-xs"}>
              Comfort control: {status.comfort_controllability.message}
              {status.comfort_controllability.outdoor_temp_c != null && status.comfort_controllability.cutoff_c != null
                ? ` Outdoor ${status.comfort_controllability.outdoor_temp_c.toFixed(1)}°C · cutoff ${status.comfort_controllability.cutoff_c.toFixed(1)}°C.`
                : ""}
            </p>
          )}
        </section>
        <section><h3>Seasonal calibration</h3>{status.seasonal_calibration ? (
          <p className={status.seasonal_calibration.observe_only_active ? "text-warning text-sm" : "text-muted text-xs"}>
            Seasonal calibration: {status.seasonal_calibration.reason === "awaiting_explicit_seasonal_cycle" || status.seasonal_calibration.reason === "seasonal_reporting_unavailable" ? readinessReasonLabel(status.seasonal_calibration.reason) : seasonalReasonLabel(status.seasonal_calibration.reason)}
            {status.seasonal_calibration.average_outdoor_c != null ? ` · recent outdoor average ${status.seasonal_calibration.average_outdoor_c.toFixed(1)}°C` : ""}
            {status.seasonal_calibration.observe_only_active && " — device commands are paused while natural heating data is collected."}
            {status.seasonal_calibration.next_step ? ` Next: ${reasonLabel(status.seasonal_calibration.next_step)}.` : ""}
          </p>
        ) : <p className="optimizer-readable-text">Unavailable.</p>}
          {status.seasonal_calibration && (
            <div data-testid="seasonal-reporting-summary" className="text-muted text-xs" style={{ margin: "0.75rem 0", display: "block", minWidth: 0, overflowWrap: "anywhere" }}>
              <strong>Season readiness</strong>
              {readiness ? <>
                <div>As of: {readiness.as_of ? formatDate(readiness.as_of) : "Unknown"}; Generation: {readiness.generation?.slice(0, 8) ?? "Unknown"}; Context: {readiness.context_digest?.slice(0, 8) ?? "Unknown"}</div>
                {demandStage && <ReadinessDetails name="demand" label="Demand collection" stage={demandStage} />}
                {indoorStage && <ReadinessDetails name="indoor_heating" label="Indoor-heating collection" stage={indoorStage} />}
                {readiness.components.indoor_cooling && <ReadinessDetails name="indoor_cooling" label="Indoor-cooling collection" stage={readiness.components.indoor_cooling} />}
                <div data-testid="readiness-maintenance" style={{ marginTop: "0.75rem" }}>
                  <div>{readiness.maintenance.enabled === false ? "Model maintenance is disabled." : readiness.maintenance.enabled === true ? "Model maintenance is enabled." : "Model maintenance status is unknown."}</div>
                  <div>Maintenance preference: {readiness.maintenance.preference}.</div>
                  <div>Queued: {readiness.maintenance.queue_count ?? "Unknown"}; Running: {readiness.maintenance.running === true ? "Yes" : readiness.maintenance.running === false ? "No" : "Unknown"}; Phase: {readiness.maintenance.phase ?? "Unknown"}</div>
                  <div>Last attempt: {readiness.maintenance.last_attempt_at ? formatDate(readiness.maintenance.last_attempt_at) : "Unknown"}; Next due: {readiness.maintenance.next_due_at ? formatDate(readiness.maintenance.next_due_at) : "Unknown"}</div>
                  {readiness.maintenance.reason && <div>{readinessReasonLabel(readiness.maintenance.reason)}</div>}
                </div>
              </> : <>
                <div>Demand evidence: {status.seasonal_calibration.demand?.usable_samples ?? "Unknown"}/{status.seasonal_calibration.demand?.minimum_samples ?? "Unknown"}</div>
                <div>Indoor-heating collection: unavailable.</div>
                <div>{lifecycleStatuses.demand?.maintenance_enabled === false ? "Model maintenance is disabled." : lifecycleStatuses.demand?.maintenance_enabled === true ? "Model maintenance is enabled." : "Model maintenance status is unknown."}</div>
                <div>Maintenance preference: {status.seasonal_calibration.auto_train === true ? "automatic" : status.seasonal_calibration.auto_train === false ? "manual" : "unknown"}.</div>
              </>}
            </div>
          )}</section>
        <section><h3>Sensor diagnostics</h3>{sensorDiagnostics ? (
          <div className="indoor-temp-card text-muted text-xs" style={{ margin: "0.75rem 0" }}>
            <strong>Sensor diagnostics · shadow mode</strong>
            <div>{sensorDiagnostics.summary} {sensorDiagnostics.room_spread_c != null ? `Room spread ${sensorDiagnostics.room_spread_c.toFixed(1)}°C.` : ""}</div>
            <div>{sensorDiagnostics.sensors.map((sensor) => `${sensor.label || sensor.room || sensor.device_id}: ${sensor.state}${sensor.is_reference ? " (comfort reference)" : ""}`).join(" · ")}</div>
          </div>
        ) : <p className="optimizer-readable-text">Unavailable.</p>}</section>

        {spaceHeatingGate ? (
          <section>
            <h3>Space-heating gate</h3>
            <p className="optimizer-readable-text">{spaceHeatingGate.state}: {spaceHeatingGate.reason}. Profile: {spaceHeatingGate.profile_id}. Thresholds: {spaceHeatingGate.on_threshold_c} C on, {spaceHeatingGate.off_threshold_c} C off.</p>
          </section>
        ) : <section><h3>Space-heating gate</h3><p className="optimizer-readable-text">Unavailable.</p></section>}

        <section>
          <h3>COP retired</h3>
          <p className="optimizer-readable-text">COP training and inference are retired because thermal-output evidence is unverified. Archived estimates are retained; planning uses assumed heating efficiency, not measured COP.</p>
          <p className="optimizer-readable-text">Total COP is available manually at the heat pump&apos;s Panasonic controller (&quot;Totalt COP-värde&quot;, marked &quot;Ungefär&quot;). This approximate whole-system value is separate from the retired DHW model and is not an optimizer input.</p>
        </section>

        <ManualCOPMonitoring />

        {/* Model cards */}
        <section><h3>Models</h3><div className="model-grid">
          {models.map((m) => (
            <div key={m.label} className="model-card">
              <div className="model-card-header" style={{ flexWrap: "wrap" }}>
                <StatusDot state={m.state} />
                <h4 className="model-card-name">{m.label}</h4>
                <span className={`model-card-badge model-card-badge--${m.state === "qualified-for-reporting" ? "validated" : m.state === "candidate" ? "trained" : "collecting"}`}>{modelStateLabel(m.state)}</span>
              </div>
              <div className="model-card-plain">{m.plain}</div>
              <div className="model-card-details">
                <div>Last trained: {formatDate(m.lastTrained)}</div>
                <div>{m.detail}</div>
                {m.unavailableReason && <div>{m.unavailableReason}</div>}
                {m.capabilities && (
                  <div className="model-capabilities">
                    {m.capabilities.map((capability) => (
                      <span key={capability.label} className={`model-capability model-capability--${capability.state}`}>
                        {capability.label}: {capability.state}
                      </span>
                    ))}
                  </div>
                )}
                <div className="model-next-step"><strong>What this means:</strong> {m.nextStep}</div>
              </div>
              <LifecycleDetails component={m.component} status={m.lifecycle} maintenance={readiness?.maintenance} />
            </div>
          ))}
        </div></section>

        <section><h3>Forecast validation</h3>{forecastScorecard ? (
          <div className="indoor-temp-card" style={{ marginTop: "1rem" }}>
            <div className="model-card-header">
              <span className="model-card-name">Forecast validation</span>
            </div>
            <div className="model-card-details">
              {forecastFamilies.map((family) => {
                const evidence = family.evidence;
                const gate = evidence?.quality_gate;
                return (
                  <div key={family.label} style={{ marginTop: "0.6rem" }}>
                    <h4>{family.label}</h4>
                    <div className="text-muted text-xs">{family.detail}</div>
                    {!evidence || evidence.overall.samples === 0 ? (
                      <div className="text-muted text-xs">Waiting for clean, sensor-matched outcomes.</div>
                    ) : (
                      <div>
                        MAE {evidence.overall.mae?.toFixed(2)}°C across {evidence.overall.samples} outcomes
                        {evidence.overall.bias != null ? ` · bias ${evidence.overall.bias >= 0 ? "+" : ""}${evidence.overall.bias.toFixed(2)}°C` : ""}
                        {evidence.overall.p90_abs_error != null ? ` · P90 ${evidence.overall.p90_abs_error.toFixed(2)}°C` : ""}
                        <div className={gate?.control_allowed ? "text-muted text-xs" : "text-warning text-sm"}>
                          {gate?.status === "failed" ? "Quality gate: " : "Evidence: "}{reasonLabel(gate?.reason)}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
              <div className="text-muted text-xs" style={{ marginTop: "0.6rem" }}>{forecastScorecard.note}</div>
              {forecastScorecard.exclusions && Object.values(forecastScorecard.exclusions).some((count) => count > 0) && (
                <div className="text-muted text-xs">Excluded outcomes: {Object.entries(forecastScorecard.exclusions).map(([reason, count]) => `${count} ${reasonLabel(reason)}`).join(" · ")}</div>
              )}
            </div>
          </div>
        ) : <p className="optimizer-readable-text">Unavailable.</p>}</section>

        {/* Training controls */}
        <section><h3>Training</h3><div className="training-controls">
          <button
            className="btn btn-sm"
            onClick={trainMl}
            disabled={training.ml || demandTrainingBlocked}
            aria-busy={training.ml}
            title={demandTrainingBlocked ? "Demand training is waiting for usable heating-season evidence." : undefined}
          >
            {training.ml ? "Training..." : "Train Demand"}
          </button>
          <button
            className="btn btn-sm"
            onClick={trainComfort}
            disabled={training.comfort || comfortTrainingBlocked}
            aria-busy={training.comfort}
            title={comfortTrainingBlocked ? "Reporting qualification requires confirmed room-heating outcomes." : undefined}
          >
            {training.comfort ? "Training..." : comfortTrainingBlocked ? "Comfort training waiting for heat samples" : "Train Comfort Model"}
          </button>
          <button
            className="btn btn-sm"
            onClick={calibrateThermal}
            disabled={training.thermal}
            aria-busy={training.thermal}
          >
            {training.thermal ? "Calibrating..." : "Calibrate Thermal"}
          </button>
        </div>
          {trainMsg && (
            <p className={`train-msg train-msg--${trainMsg.tone}`}>
              {trainMsg.text}
            </p>
          )}</section>

        {/* SmartThings indoor temperature */}
        <section><h3>SmartThings indoor temperature</h3>{indoorTemp && indoorTemp.avg_temperature != null ? (
          <>
            <div className="indoor-temp-card">
              <div className="indoor-temp-value">
                {indoorTemp.avg_temperature.toFixed(1)}°C
              </div>
              <div className="model-card-details">
                <div>
                  {indoorTemp.reference_sensor_id
                    ? `Reference room: ${indoorTemp.reference_sensor_label || indoorTemp.reference_room || indoorTemp.reference_sensor_id}`
                    : `Trusted median from ${indoorTemp.sensor_count} sensor${indoorTemp.sensor_count !== 1 ? "s" : ""}`}
                  {indoorTemp.confidence ? ` · ${indoorTemp.confidence} confidence` : ""}
                  {indoorTemp.spread_c != null ? ` · room spread ${indoorTemp.spread_c.toFixed(1)}°C` : ""}
                </div>
                <div>Last reading: {formatDate(indoorTemp.latest_reading)}</div>
                {indoorTemp.sensors && indoorTemp.sensors.length > 1 && (
                  <div>
                    Rooms: {indoorTemp.sensors.map((sensor) => `${sensor.device_label || sensor.room || sensor.device_id} ${sensor.temperature.toFixed(1)}°C`).join(" · ")}
                  </div>
                )}
                {indoorTemp.reason && <div className="text-warning text-sm">{reasonLabel(indoorTemp.reason)}</div>}
                {indoorTemp.last_fresh_reading && indoorTemp.last_fresh_reading !== indoorTemp.latest_reading && (
                  <div className="text-warning text-sm">
                    Sensor data stale: last fresh reading {formatDate(indoorTemp.last_fresh_reading)}
                  </div>
                )}
                {!indoorTemp.last_fresh_reading && (
                  <div className="text-warning text-sm">
                    No fresh sensor data received yet
                  </div>
                )}
              </div>
            </div>
          </>
        ) : <p className="optimizer-readable-text">Unavailable.</p>}</section>
      </OptimizerDiagnostics>
    </div>
  );
}
