"use client";

import { useEffect, useState } from "react";
import { LAYER_LABELS } from "@/lib/constants";

interface ModelInfo {
  trained: boolean;
  last_trained: string | null;
  samples?: number;
  source_records?: number;
  metrics?: { mae?: number; cv_std?: number; samples?: number; baseline_mae?: number; validation_method?: string };
  data_quality?: DemandDataQuality;
}

interface DemandDataQuality {
  raw_records: number;
  intervals: number;
  usable_samples: number;
  minimum_samples: number;
  rejected_nonpositive: number;
  rejected_rate_bounds: number;
  weather_matches: number;
  remaining_samples?: number;
  heating_activity_ratio?: number;
  training_blocker?: string | null;
  seasonal_guidance?: string;
  ready_to_train: boolean;
}

interface ModelTrainResult {
  error?: string;
  samples?: number;
  version?: string;
}

interface ThermalInfo {
  calibrated: boolean;
  tank_heating_rate: number;
  confidence: string;
  indoor_heating_confidence: string;
  indoor_heating_samples: number;
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

interface OptimizerStatusData {
  configured_layer: string;
  active_layer: string;
  fallback_layer: string;
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
    enabled: boolean;
    observe_only_active: boolean;
    heating_season_detected: boolean;
    reason: string;
    average_outdoor_c: number | null;
    next_step?: string;
    auto_train?: boolean;
    auto_exit?: boolean;
    demand?: { usable_samples?: number; minimum_samples?: number; remaining_samples?: number; trained?: boolean };
    indoor_heating?: { samples?: number; minimum_samples?: number; remaining_samples?: number };
  };
  decision_readiness?: { state: "ready" | "collecting" | "fallback"; title: string; detail: string };
  cop_model: ModelInfo;
  demand_model: ModelInfo;
  thermal_model: ThermalInfo;
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

type ModelState = "active" | "validated" | "trained" | "collecting";

interface ModelCard {
  label: string;
  plain: string;
  state: ModelState;
  lastTrained: string | null;
  detail: string;
  nextStep: string;
  capabilities?: Array<{ label: string; state: "ready" | "learning" | "waiting" }>;
}

interface TrainMessage {
  text: string;
  tone: "success" | "info" | "error";
}

function StatusDot({ state }: { state: ModelState }) {
  return (
    <span className={`status-dot ${state === "active" || state === "validated" ? "status-dot--ok" : state === "trained" ? "status-dot--observing" : ""}`} />
  );
}

function modelStateLabel(state: ModelState): string {
  if (state === "active") return "Active";
  if (state === "validated") return "Validated";
  if (state === "trained") return "Trained · not approved";
  return "Collecting evidence";
}

function formatDate(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "unknown";
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function layerBadgeClass(layer: string): string {
  if (layer.includes("ml")) return "opt-layer-badge opt-layer-badge--ml";
  if (layer.includes("milp")) return "opt-layer-badge opt-layer-badge--milp";
  return "opt-layer-badge";
}

function formatAge(seconds: number | null): string {
  if (seconds == null) return "no device status";
  if (seconds < 60) return "just now";
  return `${Math.round(seconds / 60)} min ago`;
}

export function OptimizerStatus() {
  const [status, setStatus] = useState<OptimizerStatusData | null>(null);
  const [comfort, setComfort] = useState<ComfortInfo | null>(null);
  const [indoorTemp, setIndoorTemp] = useState<IndoorTempLatest | null>(null);
  const [forecastScorecard, setForecastScorecard] = useState<ForecastScorecard | null>(null);
  const [sensorDiagnostics, setSensorDiagnostics] = useState<SensorDiagnostics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [training, setTraining] = useState<Record<string, boolean>>({});
  const [trainMsg, setTrainMsg] = useState<TrainMessage | null>(null);

  const refresh = () =>
    Promise.all([
      fetch("/api/optimizer/status").then((r) => (r.ok ? r.json() : null)),
      fetch("/api/comfort-model/status").then((r) => (r.ok ? r.json() : null)),
      fetch("/api/indoor-temp/latest").then((r) => (r.ok ? r.json() : null)),
      fetch("/api/thermal/forecast-scorecard").then((r) => (r.ok ? r.json() : null)),
      fetch("/api/sensors/diagnostics").then((r) => (r.ok ? r.json() : null)),
    ])
      .then(([opt, cmf, temp, scorecard, diagnostics]) => {
        setStatus(opt);
        setComfort(cmf);
        setIndoorTemp(temp);
        setForecastScorecard(scorecard);
        setSensorDiagnostics(diagnostics);
      })
      .catch(() => setError("Failed to load optimizer status"));

  useEffect(() => { refresh(); }, []);

  const trainMl = async () => {
    setTraining((p) => ({ ...p, ml: true }));
    setTrainMsg(null);
    try {
      const res = await fetch("/api/ml/train", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Training failed");
      const fmtResult = (r: ModelTrainResult | null | undefined) =>
        r?.error ? `✗ ${r.error} (${r.samples ?? 0} samples)` : r?.version ? `✓ trained` : "unknown";
      const copStatus = fmtResult(data.cop);
      const demandStatus = fmtResult(data.demand);
      setTrainMsg({ text: `COP: ${copStatus} · Demand: ${demandStatus}`, tone: !data.cop?.error && !data.demand?.error ? "success" : "error" });
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
      const reason = typeof data.control_readiness?.reason === "string"
        ? data.control_readiness.reason.replace(/_/g, " ")
        : "validation is still pending";
      setTrainMsg({
        text: data.control_ready
          ? "Comfort model trained and approved for indoor-temperature control."
          : `Comfort model trained — observation only (${reason}).`,
        tone: data.control_ready ? "success" : "info",
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
      setTrainMsg({ text: "Thermal model calibrated", tone: "success" });
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
  const dataFreshness = status.data_freshness ?? {
    latest_device_status: null,
    age_seconds: null,
    stale_after_seconds: 0,
    fresh: false,
  };
  const demandDetail = demandQuality
    ? `${demandQuality.usable_samples}/${demandQuality.minimum_samples} usable intervals${demandQuality.remaining_samples ? ` · ${demandQuality.remaining_samples} still needed` : ""}${demandQuality.training_blocker === "waiting_for_space_heating_season" ? " · waiting for heating season" : ""}${demandQuality.rejected_rate_bounds > 0 ? ` · ${demandQuality.rejected_rate_bounds} rejected as implausible rate` : ""}${demandQuality.weather_matches > 0 ? ` · weather matched ${demandQuality.weather_matches}` : ""}`
    : `${status.demand_model.samples ?? 0} usable intervals`;

  const models: ModelCard[] = [
    {
      label: "COP Model",
      plain: "Predicts how efficiently the pump runs at different temperatures",
      state: status.cop_model.trained
        ? (
            status.cop_model.metrics?.mae != null
            && status.cop_model.metrics?.baseline_mae != null
            && status.cop_model.metrics.mae < status.cop_model.metrics.baseline_mae
              ? "validated"
              : "trained"
          )
        : "collecting",
      lastTrained: status.cop_model.last_trained,
      detail: `${status.cop_model.source_records ?? 0} energy readings${status.cop_model.metrics?.mae != null ? ` · forward CV MAE ${status.cop_model.metrics.mae.toFixed(3)} COP${status.cop_model.metrics.cv_std != null ? ` · ±${status.cop_model.metrics.cv_std.toFixed(3)}` : ""}` : " · validation score will appear after the next training run"}`,
      nextStep: status.cop_model.trained
        ? status.cop_model.metrics?.baseline_mae != null && status.cop_model.metrics?.mae != null && status.cop_model.metrics.mae >= status.cop_model.metrics.baseline_mae
          ? "The learned model does not yet beat the simple baseline, so it is not promoted as validated."
          : "Keep collecting varied outdoor-temperature and load observations."
        : "Train after enough matched consumption and weather records have accumulated.",
    },
    {
      label: "Demand Model",
      plain: demandQuality?.seasonal_guidance ?? "Forecasts how much hot water and heating you'll need",
      state: status.demand_model.trained
        ? (status.active_layer.includes("ml") ? "active" : "validated")
        : "collecting",
      lastTrained: status.demand_model.last_trained,
      detail: demandDetail,
      nextStep: status.demand_model.trained
        ? "Used only when the selected decision layer and input-quality gates allow it."
        : demandQuality?.training_blocker === "waiting_for_space_heating_season"
          ? "Wait for heating weather; manual retraining cannot create the missing evidence."
          : `${demandQuality?.remaining_samples ?? 0} more usable heating interval(s) are needed.`,
    },
    {
      label: "Thermal Model",
      plain: "Learns how fast your tank and rooms heat up",
      state: status.thermal_model.calibrated
        ? (status.thermal_model.indoor_heating_confidence === "learned" ? "validated" : "trained")
        : "collecting",
      lastTrained: status.thermal_model.last_calibrated,
      detail: `Tank: ${status.thermal_model.tank_heating_rate} °C/h · ${status.thermal_model.confidence} · Indoor heat: ${status.thermal_model.indoor_heating_confidence} (${status.thermal_model.indoor_heating_samples} samples)${status.thermal_model.calibration_status?.zone_cooling ? ` · Zone cooling: ${status.thermal_model.calibration_status.zone_cooling.replace(/_/g, " ")}` : ""}`,
      nextStep: status.thermal_model.indoor_heating_confidence === "learned"
        ? "Tank and indoor response have learned evidence."
        : "Tank calibration is available, but indoor heating still uses safe defaults until confirmed heating samples exist.",
      capabilities: [
        { label: "Tank response", state: status.thermal_model.calibrated ? "ready" : "waiting" },
        { label: "Indoor heating", state: status.thermal_model.indoor_heating_confidence === "learned" ? "ready" : status.thermal_model.indoor_heating_samples > 0 ? "learning" : "waiting" },
      ],
    },
  ];

  if (comfort) {
    const metrics = comfort.metrics ?? {};
    const mae = typeof metrics.mae === "number" ? metrics.mae.toFixed(3) : null;
    const r2 = typeof metrics.r2 === "number" ? metrics.r2.toFixed(3) : null;
    const persistenceMae = typeof metrics.baseline_mae === "number" ? metrics.baseline_mae.toFixed(3) : null;
    const activeRows = typeof metrics.active_heating_rows === "number" ? metrics.active_heating_rows : null;
    const horizon = typeof metrics.training_horizon_minutes === "number" ? metrics.training_horizon_minutes : null;
    const sensorCount = typeof metrics.source_sensor_count === "number" ? metrics.source_sensor_count : null;
    const sensorStrategy = typeof metrics.sensor_strategy === "string" ? metrics.sensor_strategy.replace(/_/g, " ") : null;
    const metricsStr = [
      mae && `MAE ${mae}°C`,
      r2 && `R² ${r2}`,
      persistenceMae && `persistence MAE ${persistenceMae}°C`,
      activeRows !== null && `${activeRows} confirmed heating samples`,
      horizon && `${horizon}-min horizon`,
      sensorStrategy && `${sensorStrategy}${sensorCount !== null ? ` (${sensorCount} sensor${sensorCount === 1 ? "" : "s"})` : ""}`,
    ].filter(Boolean).join(" · ");
    models.push({
      label: "Comfort Model",
      plain: comfort.control_ready
        ? "Validated and used for indoor-temperature control"
        : comfort.trained
          ? "Trained, but observation only until validation quality is sufficient"
          : comfort.training_notice ?? "Collecting data before indoor-temperature training can begin",
      state: comfort.control_ready ? "validated" : comfort.trained ? "trained" : "collecting",
      lastTrained: comfort.last_trained,
      detail: `${comfort.training_samples} samples${metricsStr ? ` · ${metricsStr}` : ""}${comfort.control_margin_c ? ` · ${comfort.control_margin_c.toFixed(2)}°C planning reserve` : ""}${comfort.control_readiness?.reason ? ` · ${comfort.control_readiness.reason.replace(/_/g, " ")}` : ""}`,
      nextStep: comfort.control_ready
        ? "Validated for control; the active decision layer still decides whether it is used."
        : comfort.trained
          ? "Observation only. It will not control the pump until forecast validation passes."
          : comfort.training_notice ?? "Collect more clean sensor-matched observations before training.",
    });
  }

  const activeLayerLabel = LAYER_LABELS[status.active_layer] || status.active_layer;
  const demandTrainingBlocked = Boolean(status.cop_model.trained && demandQuality && !demandQuality.ready_to_train);
  const comfortActiveHeatingRows = typeof comfort?.metrics?.active_heating_rows === "number"
    ? comfort.metrics.active_heating_rows
    : 0;
  const comfortTrainingBlocked = Boolean(comfort?.trained && comfortActiveHeatingRows === 0);
  const forecastFamilies = forecastScorecard ? [
    {
      label: "Comfort model · control evidence",
      detail: "Only this validated heating model can approve ML comfort control.",
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
    ? `Automatic mode — currently using ${activeLayerLabel}, the safest available layer.`
    : `Fixed setting — using ${activeLayerLabel}.`;
  const demandProgress = status.seasonal_calibration?.demand?.minimum_samples
    ? Math.min(100, Math.round(100 * (status.seasonal_calibration.demand.usable_samples ?? 0) / status.seasonal_calibration.demand.minimum_samples))
    : null;
  const indoorHeatingProgress = status.seasonal_calibration?.indoor_heating?.minimum_samples
    ? Math.min(100, Math.round(100 * (status.seasonal_calibration.indoor_heating.samples ?? 0) / status.seasonal_calibration.indoor_heating.minimum_samples))
    : null;

  return (
    <div className="plan-section">
      <h2 className="chart-title">How the optimizer is deciding</h2>
      <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", marginBottom: "1rem" }}>
        The active layer chooses your schedule. Optional machine-learning models make it smarter as
        they collect data — until they&apos;re ready, the optimizer falls back to safe built-in rules.
      </p>

      {/* Active layer badge */}
      <div className="opt-layer-row">
        <span className="text-muted text-sm">Decision engine</span>
        <span className={layerBadgeClass(status.active_layer)}>
          {activeLayerLabel}
        </span>
        <span className="text-muted text-xs">
          {layerExplanation}
        </span>
      </div>
      <p className={dataFreshness.fresh ? "text-muted text-xs" : "text-warning text-sm"}>
        Live pump status: {formatAge(dataFreshness.age_seconds)}
        {!dataFreshness.fresh && " — automatic commands are paused until fresh data returns."}
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
      {status.seasonal_calibration && (
        <p className={status.seasonal_calibration.observe_only_active ? "text-warning text-sm" : "text-muted text-xs"}>
          Seasonal calibration: {status.seasonal_calibration.reason.replace(/_/g, " ")}
          {status.seasonal_calibration.average_outdoor_c != null ? ` · recent outdoor average ${status.seasonal_calibration.average_outdoor_c.toFixed(1)}°C` : ""}
          {status.seasonal_calibration.observe_only_active && " — device commands are paused while natural heating data is collected."}
          {status.seasonal_calibration.next_step ? ` Next: ${status.seasonal_calibration.next_step.replace(/_/g, " ")}.` : ""}
        </p>
      )}
      {status.seasonal_calibration?.enabled && (
        <div className="indoor-temp-card text-muted text-xs" style={{ margin: "0.75rem 0" }}>
          <strong>Season readiness</strong>
          <div>Demand evidence: {status.seasonal_calibration.demand?.usable_samples ?? 0}/{status.seasonal_calibration.demand?.minimum_samples ?? "—"} {demandProgress != null ? `(${demandProgress}%)` : ""}</div>
          {demandProgress != null && <progress value={demandProgress} max={100} aria-label="Demand-model seasonal evidence" style={{ width: "100%" }} />}
          <div>Indoor-heating evidence: {status.seasonal_calibration.indoor_heating?.samples ?? 0}/{status.seasonal_calibration.indoor_heating?.minimum_samples ?? "—"} {indoorHeatingProgress != null ? `(${indoorHeatingProgress}%)` : ""}</div>
          {indoorHeatingProgress != null && <progress value={indoorHeatingProgress} max={100} aria-label="Indoor-heating seasonal evidence" style={{ width: "100%" }} />}
          <div>{status.seasonal_calibration.auto_train ? "Automatic training is enabled when evidence is ready." : "Manual training is selected."}</div>
        </div>
      )}
      {sensorDiagnostics && (
        <div className="indoor-temp-card text-muted text-xs" style={{ margin: "0.75rem 0" }}>
          <strong>Sensor diagnostics · shadow mode</strong>
          <div>{sensorDiagnostics.summary} {sensorDiagnostics.room_spread_c != null ? `Room spread ${sensorDiagnostics.room_spread_c.toFixed(1)}°C.` : ""}</div>
          <div>{sensorDiagnostics.sensors.map((sensor) => `${sensor.label || sensor.room || sensor.device_id}: ${sensor.state}${sensor.is_reference ? " (comfort reference)" : ""}`).join(" · ")}</div>
        </div>
      )}

      {/* Model cards */}
      <div className="model-grid">
        {models.map((m) => (
          <div key={m.label} className="model-card">
            <div className="model-card-header">
              <StatusDot state={m.state} />
              <span className="model-card-name">{m.label}</span>
              <span className={`model-card-badge model-card-badge--${m.state}`}>{modelStateLabel(m.state)}</span>
            </div>
            <div className="model-card-plain">{m.plain}</div>
            <div className="model-card-details">
              <div>Last trained: {formatDate(m.lastTrained)}</div>
              <div>{m.detail}</div>
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
          </div>
        ))}
      </div>

      {forecastScorecard && (
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
                  <strong>{family.label}</strong>
                  <div className="text-muted text-xs">{family.detail}</div>
                  {!evidence || evidence.overall.samples === 0 ? (
                    <div className="text-muted text-xs">Waiting for clean, sensor-matched outcomes.</div>
                  ) : (
                    <div>
                      MAE {evidence.overall.mae?.toFixed(2)}°C across {evidence.overall.samples} outcomes
                      {evidence.overall.bias != null ? ` · bias ${evidence.overall.bias >= 0 ? "+" : ""}${evidence.overall.bias.toFixed(2)}°C` : ""}
                      {evidence.overall.p90_abs_error != null ? ` · P90 ${evidence.overall.p90_abs_error.toFixed(2)}°C` : ""}
                      <div className={gate?.control_allowed ? "text-muted text-xs" : "text-warning text-sm"}>
                        {gate?.status === "failed" ? "Quality gate: " : "Evidence: "}{gate?.reason?.replace(/_/g, " ")}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
            <div className="text-muted text-xs" style={{ marginTop: "0.6rem" }}>{forecastScorecard.note}</div>
            {forecastScorecard.exclusions && Object.values(forecastScorecard.exclusions).some((count) => count > 0) && (
              <div className="text-muted text-xs">Excluded outcomes: {Object.entries(forecastScorecard.exclusions).map(([reason, count]) => `${count} ${reason.replace(/_/g, " ")}`).join(" · ")}</div>
            )}
          </div>
        </div>
      )}

      {/* Training controls */}
      <div className="training-controls">
        <button
          className="btn btn-sm"
          onClick={trainMl}
          disabled={training.ml || demandTrainingBlocked}
          aria-busy={training.ml}
          title={demandTrainingBlocked ? "Demand training is waiting for usable heating-season evidence." : undefined}
        >
          {training.ml ? "Training..." : demandTrainingBlocked ? "Demand training waiting for evidence" : "Train COP & Demand"}
        </button>
        <button
          className="btn btn-sm"
          onClick={trainComfort}
          disabled={training.comfort || comfortTrainingBlocked}
          aria-busy={training.comfort}
          title={comfortTrainingBlocked ? "Retraining cannot improve control readiness until confirmed room-heating samples exist." : undefined}
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
      )}

      {/* SmartThings indoor temperature */}
      {indoorTemp && indoorTemp.avg_temperature != null && (
        <>
          <h3 className="indoor-temp-heading">SmartThings Indoor Temperature</h3>
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
              {indoorTemp.reason && <div className="text-warning text-sm">⚠ {indoorTemp.reason.replace(/_/g, " ")}</div>}
              {indoorTemp.last_fresh_reading && indoorTemp.last_fresh_reading !== indoorTemp.latest_reading && (
                <div className="text-warning text-sm">
                  ⚠ Sensor data stale — last fresh reading: {formatDate(indoorTemp.last_fresh_reading)}
                </div>
              )}
              {!indoorTemp.last_fresh_reading && (
                <div className="text-warning text-sm">
                  ⚠ No fresh sensor data received yet
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
