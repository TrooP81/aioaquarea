"use client";

import { useEffect, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
  ReferenceLine,
} from "recharts";

interface ThermalStatus {
  current: {
    tank_temp: number;
    tank_target: number;
    outdoor_temp: number;
    zone1_temp: number;
    timestamp: string;
  };
  predictions: {
    tank_heating: {
      minutes_to_target: number;
      heating_rate_per_hour: number;
      confidence: string;
    };
    tank_cooling: {
      minutes_until_min: number | null;
      loss_rate_per_hour: number;
      confidence: string;
    };
    zone_boost: {
      minutes_for_2deg: number;
      heating_rate_per_hour: number;
      confidence: string;
    };
    indoor?: {
      current_indoor_temp: number | null;
      minutes_to_cool_2deg: number | null;
      minutes_to_heat_1deg: number | null;
      indoor_heating_rate: number;
      indoor_cooling_rate: number;
      indoor_heating_samples: number;
      indoor_cooling_samples: number;
      confidence: string;
      reason?: string | null;
    };
  };
  model_params: {
    tank_heating_rate: number;
    tank_standby_loss: number;
    zone_heating_rate: number;
    last_calibrated: string | null;
    sample_count: number;
  };
}

interface CurveData {
  current: {
    tank_temp: number;
    tank_target: number;
    outdoor_temp: number;
    zone1_temp: number;
    plan_driven?: boolean;
    learning_mode?: boolean;
    plan_id?: number | null;
  };
  curves: {
    tank_standby: { hour: number; predicted_temp: number; state: string }[];
    tank_heating: { hour: number; predicted_temp: number; state: string }[];
    zone_standby: { hour: number; predicted_temp: number; state: string }[];
  };
}

interface PlannedAction {
  hour: number;
  action_type: string;
  status: string;
  payload: Record<string, unknown>;
}

interface IndoorForecastData {
  current_indoor: number | null;
  outdoor_temp: number | null;
  forecast: { hour: number; ts?: string; predicted_indoor_temp: number; source?: string | null; model_source?: string | null }[];
  forecast_with_plan: { hour: number; ts?: string; predicted_indoor_temp: number; source?: string | null; model_source?: string | null; space_heating_fraction?: number | null }[];
  forecast_no_heating: { hour: number; ts?: string; predicted_indoor_temp: number }[];
  target_schedule: { hour: number; ts?: string; target: number; comfort_hour: boolean }[];
  planned_actions: PlannedAction[];
  forecast_status?: "available" | "unavailable";
  forecast_unavailable_reason?: string | null;
  comfort_assessment?: {
    state: "on_target" | "at_risk" | "unavailable";
    summary: string;
    controllability?: { status: string; cutoff_c?: number };
    first_miss?: { ts?: string; predicted_c: number; target_c: number; shortfall_c: number; outdoor_temp_c?: number | null; model_source?: string | null; prediction_interval_status?: string | null };
    worst_miss?: { shortfall_c: number };
    recommendations?: { title: string; summary: string; manual_only: boolean; current_value_c?: number; minimum_candidate_value_c?: number; expected_effect?: string; confidence?: string; verification_required?: boolean }[];
  };
  forecast_provenance?: { current_live_indoor_c?: number | null; plan_start_indoor_c?: number | null; plan_created_at?: string; control_input?: { reference_sensor_label?: string | null; observed_at?: string | null; confidence?: string | null } };
}

interface IndoorTempReading {
  timestamp: string;
  temperature: number;
}

export function ThermalPredictionChart() {
  const [status, setStatus] = useState<ThermalStatus | null>(null);
  const [curves, setCurves] = useState<CurveData | null>(null);
  const [indoorForecast, setIndoorForecast] = useState<IndoorForecastData | null>(null);
  const [indoorHistory, setIndoorHistory] = useState<IndoorTempReading[]>([]);
  const [optimizing, setOptimizing] = useState(false);
  const [optimizeResult, setOptimizeResult] = useState<string | null>(null);
  const [calibrating, setCalibrating] = useState(false);
  const [calibrateResult, setCalibrateResult] = useState<string | null>(null);

  useEffect(() => {
    fetchData();
  }, []);

  const fetchData = async () => {
    try {
      const [statusRes, curveRes, indoorRes, historyRes] = await Promise.all([
        fetch("/api/thermal/status"),
        fetch("/api/thermal/curve?hours=24"),
        fetch("/api/thermal/indoor-forecast?hours=24"),
        fetch("/api/indoor-temp?hours=6"),
      ]);
      if (statusRes.ok) setStatus(await statusRes.json());
      if (curveRes.ok) setCurves(await curveRes.json());
      if (indoorRes.ok) setIndoorForecast(await indoorRes.json());
      if (historyRes.ok) setIndoorHistory(await historyRes.json());
    } catch {}
  };

  const handleCalibrate = async () => {
    setCalibrating(true);
    setCalibrateResult(null);
    try {
      const res = await fetch("/api/thermal/calibrate", { method: "POST" });
      const data = await res.json();
      if (data.status === "calibrated") {
        setCalibrateResult(
          `Calibrated from ${data.samples} samples (${data.tank_heating_samples} heating, ${data.tank_cooling_samples} cooling)`
        );
        await fetchData();
      } else {
        setCalibrateResult(data.status || "Calibration failed");
      }
    } catch {
      setCalibrateResult("Network error");
    } finally {
      setCalibrating(false);
    }
  };

  const handleOptimize = async () => {
    setOptimizing(true);
    setOptimizeResult(null);
    try {
      const res = await fetch("/api/optimize-now", { method: "POST" });
      const data = await res.json();
      if (data.status !== "queued" || typeof data.request_id !== "number") {
        throw new Error(data.message || "Could not queue optimization");
      }
      setOptimizeResult("Re-plan queued — waiting for the optimizer service");
      for (let attempt = 0; attempt < 16; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 2_000));
        const requestRes = await fetch(`/api/optimize-now/${data.request_id}`);
        if (!requestRes.ok) throw new Error("Could not read optimization status");
        const request = await requestRes.json();
        if (request.status === "completed") {
          setOptimizeResult(`Plan #${request.plan_id} activated`);
          await fetchData();
          return;
        }
        if (request.status === "failed") {
          setOptimizeResult(request.error || "No executable plan was generated");
          return;
        }
      }
      setOptimizeResult("Re-plan is still queued; it will continue in the background");
    } catch (error) {
      setOptimizeResult(error instanceof Error ? error.message : "Network error");
    } finally {
      setOptimizing(false);
    }
  };

  const chartData =
    curves?.curves.tank_standby.map((s, i) => ({
      hour: `+${s.hour}h`,
      tankStandby: s.predicted_temp,
      tankHeating: curves.curves.tank_heating[i]?.predicted_temp,
      zoneStandby: curves.curves.zone_standby[i]?.predicted_temp,
    })) || [];

  const indoorChartData = (() => {
    if (!indoorForecast) return [];

    // Bucket actual readings into hourly averages relative to now
    const now = Date.now();
    const actualByHour: Record<number, number[]> = {};
    for (const r of indoorHistory) {
      const hoursAgo = (now - new Date(r.timestamp).getTime()) / 3_600_000;
      // Map: -6h ago → hour -6, current → hour 0
      const hourBucket = Math.round(-hoursAgo);
      if (hourBucket >= -6 && hourBucket <= 0) {
        if (!actualByHour[hourBucket]) actualByHour[hourBucket] = [];
        actualByHour[hourBucket].push(r.temperature);
      }
    }

    // Build history points (negative hours)
    const historyPoints = [];
    for (let h = -6; h <= 0; h++) {
      const temps = actualByHour[h];
      if (temps && temps.length > 0) {
        const avg = temps.reduce((a, b) => a + b, 0) / temps.length;
        historyPoints.push({
      hour: `${h}h`,
          ts: undefined as string | undefined,
          actualIndoor: parseFloat(avg.toFixed(1)),
          indoorWithPlan: undefined as number | undefined,
          indoorNoHeating: undefined as number | undefined,
          comfortTarget: undefined as number | undefined,
        });
      }
    }

    // Build forecast points (positive hours)
    const forecastPoints = indoorForecast.forecast_with_plan.map((f, i) => ({
      hour: `+${f.hour}h`,
      ts: f.ts,
      actualIndoor: undefined as number | undefined,
      indoorWithPlan: f.predicted_indoor_temp,
      indoorNoHeating: indoorForecast.forecast_no_heating[i]?.predicted_indoor_temp,
      comfortTarget: indoorForecast.target_schedule[i]?.target,
    }));

    return [...historyPoints, ...forecastPoints];
  })();

  const comfortAssessment = indoorForecast?.comfort_assessment;
  const forecastProvenance = indoorForecast?.forecast_provenance;
  const firstComfortMiss = comfortAssessment?.first_miss;
  const missTime = firstComfortMiss?.ts
    ? new Intl.DateTimeFormat(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(firstComfortMiss.ts))
    : null;
  const manualAdvice = comfortAssessment?.recommendations?.[0];

  return (
    <div className="plan-section">
      <h2 className="chart-title">Thermal Predictions</h2>
      <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", marginBottom: "1rem" }}>
        Predicted temperature evolution based on learned heating/cooling rates.
        {status?.model_params.last_calibrated
          ? ` Model calibrated from ${status.model_params.sample_count} samples.`
          : " Model using defaults (calibrate to learn from your data)."}
        {curves?.current.learning_mode
          ? " 🎓 Learning mode is on — the optimizer plans but dispatches nothing, so the tank curve shows expected coasting, not the plan."
          : curves?.current.plan_driven
          ? ` Tank (with heating) follows the active plan${
              curves.current.plan_id ? ` #${curves.current.plan_id}` : ""
            }'s hot-water schedule.`
          : ""}
      </p>

      {/* Summary cards */}
      {status && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
            gap: "0.75rem",
            marginBottom: "1.5rem",
          }}
        >
          <div className="metric-card">
            <div className="metric-label">Tank → Target</div>
            <div className="metric-value">
              {status.predictions.tank_heating.minutes_to_target > 0
                ? `${Math.round(status.predictions.tank_heating.minutes_to_target)} min`
                : "At target"}
            </div>
            <div className="metric-sub">
              {status.predictions.tank_heating.heating_rate_per_hour}°C/h
              ({status.predictions.tank_heating.confidence})
            </div>
          </div>

          <div className="metric-card">
            <div className="metric-label">Tank Standby Loss</div>
            <div className="metric-value">
              {status.predictions.tank_cooling.loss_rate_per_hour}°C/h
            </div>
            <div className="metric-sub">
              {status.predictions.tank_cooling.minutes_until_min
                ? `${Math.round(status.predictions.tank_cooling.minutes_until_min / 60)}h until min`
                : "Stable"}
            </div>
          </div>

          <div className="metric-card">
            <div className="metric-label">Zone +2°C Boost</div>
            <div className="metric-value">
              {Math.round(status.predictions.zone_boost.minutes_for_2deg)} min
            </div>
            <div className="metric-sub">
              {status.predictions.zone_boost.heating_rate_per_hour}°C/h
              ({status.predictions.zone_boost.confidence})
            </div>
          </div>

          {status.predictions.indoor && (
            <div className="metric-card">
              <div className="metric-label">Indoor Temperature</div>
              <div className="metric-value">
                {status.predictions.indoor.current_indoor_temp != null
                  ? `${status.predictions.indoor.current_indoor_temp.toFixed(1)}°C`
                  : "No sensor"}
              </div>
              <div className="metric-sub">
                {status.predictions.indoor.indoor_heating_rate}°C/h heat
                {" / "}
                {status.predictions.indoor.indoor_cooling_rate}°C/h cool
                {status.predictions.indoor.indoor_heating_samples > 0
                  ? ` (${status.predictions.indoor.indoor_heating_samples} samples)`
                  : " (defaults)"}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Prediction curve chart */}
      {chartData.length > 0 && (
        <ResponsiveContainer width="100%" height={250}>
          <LineChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
            <XAxis dataKey="hour" stroke="#94a3b8" fontSize={11} interval={3} />
            <YAxis stroke="#94a3b8" fontSize={11} unit="°C" />
            <Tooltip
              contentStyle={{
                background: "#1e293b",
                border: "1px solid #334155",
                borderRadius: "8px",
              }}
              formatter={(value: number, name: string) => [`${value}°C`, name]}
            />
            <Legend />
            {curves && (
              <ReferenceLine
                y={curves.current.tank_target}
                stroke="#3b82f6"
                strokeDasharray="3 3"
                label={{ value: "Tank Target", position: "right", fontSize: 10, fill: "#3b82f6" }}
              />
            )}
            <Line
              type="monotone"
              dataKey="tankStandby"
              stroke="#ef4444"
              strokeWidth={2}
              strokeDasharray="5 5"
              dot={false}
              name="Tank (no heating)"
            />
            <Line
              type="monotone"
              dataKey="tankHeating"
              stroke="#3b82f6"
              strokeWidth={2}
              dot={false}
              name="Tank (with heating)"
            />
            <Line
              type="monotone"
              dataKey="zoneStandby"
              stroke="#f59e0b"
              strokeWidth={2}
              strokeDasharray="5 5"
              dot={false}
              name="Zone (no heating)"
            />
          </LineChart>
        </ResponsiveContainer>
      )}

      {/* Indoor temperature forecast chart */}
      {indoorForecast?.forecast_status === "unavailable" && (
        <div className="indoor-temp-card text-warning text-sm" style={{ marginTop: "1.5rem" }}>
          Indoor forecast paused: {(indoorForecast.forecast_unavailable_reason || "no_trusted_indoor_observation").replace(/_/g, " ")}. A fresh trusted room reading is required; no default temperature is used.
        </div>
      )}
      {indoorForecast?.forecast_status !== "unavailable" && indoorChartData.length > 0 && (
        <>
          <h3 style={{ color: "var(--text-muted)", fontSize: "0.95rem", marginTop: "1.5rem", marginBottom: "0.5rem" }}>
            Indoor Temperature Forecast
          </h3>
          {comfortAssessment && (
            <div
              className={comfortAssessment.state === "at_risk" ? "indoor-temp-card text-warning text-sm" : "indoor-temp-card text-muted text-sm"}
              style={{ marginBottom: "0.75rem" }}
            >
              <strong>{comfortAssessment.state === "at_risk" ? "Comfort risk" : "Comfort outlook"}</strong>
              <div>{comfortAssessment.summary}</div>
              {firstComfortMiss && (
                <div>
                  {missTime ? `${missTime}: ` : ""}{firstComfortMiss.predicted_c.toFixed(1)}°C forecast vs {firstComfortMiss.target_c.toFixed(1)}°C scheduled target ({firstComfortMiss.shortfall_c.toFixed(1)}°C below)
                  {firstComfortMiss.outdoor_temp_c != null ? ` · outdoor ${firstComfortMiss.outdoor_temp_c.toFixed(1)}°C` : ""}.
                </div>
              )}
              {comfortAssessment.controllability?.status === "mode_only_no_space_heat" && (
                <div>Normal/Eco changes the operating mode, but no space-heating effect is expected for this period.</div>
              )}
              {manualAdvice && (
                <div style={{ marginTop: "0.4rem" }}>
                  <strong>What would be required:</strong> {manualAdvice.title}. Current cutoff {manualAdvice.current_value_c?.toFixed(1)}°C; candidate minimum {manualAdvice.minimum_candidate_value_c?.toFixed(1)}°C. This is manual only and must be verified from measured outcome before another change.
                </div>
              )}
              {firstComfortMiss && !firstComfortMiss.prediction_interval_status && (
                <div className="text-muted text-xs" style={{ marginTop: "0.3rem" }}>
                  This is a point forecast without a calibrated uncertainty interval; treat any manual change as a bounded trial, not a guaranteed result.
                </div>
              )}
            </div>
          )}
          {forecastProvenance?.control_input?.reference_sensor_label && (
            <div className="text-muted text-xs" style={{ marginBottom: "0.5rem" }}>
              Plan input: {forecastProvenance.control_input.reference_sensor_label}
              {forecastProvenance.control_input.observed_at ? ` · observed ${new Date(forecastProvenance.control_input.observed_at).toLocaleString()}` : ""}.
              {forecastProvenance.current_live_indoor_c != null && forecastProvenance.plan_start_indoor_c != null
                ? ` Live ${forecastProvenance.current_live_indoor_c.toFixed(1)}°C vs plan start ${forecastProvenance.plan_start_indoor_c.toFixed(1)}°C.`
                : ""}
            </div>
          )}
          <ResponsiveContainer width="100%" height={250}>
            <LineChart data={indoorChartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
              <XAxis dataKey="hour" stroke="#94a3b8" fontSize={11} interval={3} />
              <YAxis stroke="#94a3b8" fontSize={11} unit="°C" domain={["auto", "auto"]} />
              <Tooltip
                contentStyle={{
                  background: "#1e293b",
                  border: "1px solid #334155",
                  borderRadius: "8px",
                }}
                formatter={(value: number, name: string) =>
                  value != null ? [`${Number(value).toFixed(1)}°C`, name] : ["-", name]
                }
              />
              <Legend />
              <Line
                type="monotone"
                dataKey="actualIndoor"
                stroke="#10b981"
                strokeWidth={2}
                dot={false}
                name="Actual Indoor"
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="comfortTarget"
                stroke="#6366f1"
                strokeWidth={1.5}
                strokeDasharray="4 4"
                dot={false}
                name="Scheduled indoor target"
              />
              <Line
                type="monotone"
                dataKey="indoorWithPlan"
                stroke="#f59e0b"
                strokeWidth={2}
                dot={false}
                name="📈 Predicted Indoor"
              />
              <Line
                type="monotone"
                dataKey="indoorNoHeating"
                stroke="#ef4444"
                strokeWidth={1.5}
                strokeDasharray="5 5"
                dot={false}
                name="❄️ No Heating"
              />
              {indoorForecast?.planned_actions?.map((a, i) => {
                const label =
                  a.action_type === "zone_temp_boost"
                    ? `🔥 +${(a.payload.offset as number) ?? 2}°C`
                    : a.action_type === "zone_temp_restore"
                    ? "⏹️ Restore"
                    : a.action_type === "force_dhw_on"
                    ? "🚰 DHW On"
                    : a.action_type === "force_dhw_off"
                    ? "🚰 DHW Off"
                    : a.action_type === "comfort_mode_on"
                    ? "☀️ Comfort"
                    : a.action_type === "eco_mode_on"
                    ? "🌿 Eco"
                    : a.action_type === "quiet_mode_on"
                    ? "🔇 Quiet"
                    : a.action_type === "quiet_mode_off"
                    ? "🔊 Loud"
                    : a.action_type.replace(/_/g, " ");
                const color =
                  a.action_type === "zone_temp_boost" ? "#f59e0b"
                    : a.action_type === "zone_temp_restore" ? "#94a3b8"
                    : a.action_type.includes("dhw") ? "#3b82f6"
                    : a.action_type.includes("comfort") ? "#10b981"
                    : a.action_type.includes("eco") ? "#22c55e"
                    : "#8b5cf6";
                return (
                  <ReferenceLine
                    key={`action-${i}`}
                    x={`+${a.hour}h`}
                    stroke={color}
                    strokeDasharray="3 3"
                    label={{
                      value: label,
                      position: "top",
                      fontSize: 10,
                      fill: color,
                    }}
                  />
                );
              })}
            </LineChart>
          </ResponsiveContainer>
        </>
      )}

      {/* Calibrate + Optimize buttons */}
      <div style={{ marginTop: "1rem", display: "flex", alignItems: "center", gap: "1rem", flexWrap: "wrap" }}>
        <button
          className="btn"
          onClick={handleCalibrate}
          disabled={calibrating}
          style={{ fontSize: "0.8rem" }}
        >
          {calibrating ? "Calibrating..." : "Calibrate Model"}
        </button>
        {calibrateResult && (
          <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
            {calibrateResult}
          </span>
        )}
        <button
          className="btn"
          onClick={handleOptimize}
          disabled={optimizing}
          style={{ fontSize: "0.8rem" }}
        >
          {optimizing ? "Optimizing..." : "Re-plan Now"}
        </button>
        {optimizeResult && (
          <span style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>
            {optimizeResult}
          </span>
        )}
      </div>
    </div>
  );
}
