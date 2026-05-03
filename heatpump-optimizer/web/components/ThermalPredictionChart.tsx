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
      minutes_to_heat_1deg: number;
      indoor_heating_rate: number;
      indoor_cooling_rate: number;
      indoor_heating_samples: number;
      indoor_cooling_samples: number;
      confidence: string;
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
  };
  curves: {
    tank_standby: { hour: number; predicted_temp: number; state: string }[];
    tank_heating: { hour: number; predicted_temp: number; state: string }[];
    zone_standby: { hour: number; predicted_temp: number; state: string }[];
  };
}

interface IndoorForecastData {
  current_indoor: number;
  outdoor_temp: number;
  forecast: { hour: number; predicted_temp: number }[];
  target_schedule: { hour: number; target: number; comfort_hour: boolean }[];
}

export function ThermalPredictionChart() {
  const [status, setStatus] = useState<ThermalStatus | null>(null);
  const [curves, setCurves] = useState<CurveData | null>(null);
  const [indoorForecast, setIndoorForecast] = useState<IndoorForecastData | null>(null);
  const [calibrating, setCalibrating] = useState(false);
  const [calibrateResult, setCalibrateResult] = useState<string | null>(null);

  useEffect(() => {
    fetchData();
  }, []);

  const fetchData = async () => {
    try {
      const [statusRes, curveRes, indoorRes] = await Promise.all([
        fetch("/api/thermal/status"),
        fetch("/api/thermal/curve?hours=24"),
        fetch("/api/thermal/indoor-forecast?hours=24"),
      ]);
      if (statusRes.ok) setStatus(await statusRes.json());
      if (curveRes.ok) setCurves(await curveRes.json());
      if (indoorRes.ok) setIndoorForecast(await indoorRes.json());
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

  const chartData =
    curves?.curves.tank_standby.map((s, i) => ({
      hour: `+${s.hour}h`,
      tankStandby: s.predicted_temp,
      tankHeating: curves.curves.tank_heating[i]?.predicted_temp,
      zoneStandby: curves.curves.zone_standby[i]?.predicted_temp,
    })) || [];

  const indoorChartData =
    indoorForecast?.forecast.map((f, i) => ({
      hour: `+${f.hour}h`,
      indoorPredicted: f.predicted_temp,
      comfortTarget: indoorForecast.target_schedule[i]?.target,
      comfortHour: indoorForecast.target_schedule[i]?.comfort_hour,
    })) || [];

  return (
    <div className="plan-section">
      <h2 className="chart-title">Thermal Predictions</h2>
      <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", marginBottom: "1rem" }}>
        Predicted temperature evolution based on learned heating/cooling rates.
        {status?.model_params.last_calibrated
          ? ` Model calibrated from ${status.model_params.sample_count} samples.`
          : " Model using defaults (calibrate to learn from your data)."}
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
      {indoorChartData.length > 0 && (
        <>
          <h3 style={{ color: "var(--text-muted)", fontSize: "0.95rem", marginTop: "1.5rem", marginBottom: "0.5rem" }}>
            Indoor Temperature Forecast
          </h3>
          <ResponsiveContainer width="100%" height={220}>
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
                formatter={(value: number, name: string) => [`${value.toFixed(1)}°C`, name]}
              />
              <Legend />
              <Line
                type="monotone"
                dataKey="comfortTarget"
                stroke="#6366f1"
                strokeWidth={1.5}
                strokeDasharray="4 4"
                dot={false}
                name="Comfort Target"
              />
              <Line
                type="monotone"
                dataKey="indoorPredicted"
                stroke="#ec4899"
                strokeWidth={2}
                dot={false}
                name="Predicted Indoor"
              />
            </LineChart>
          </ResponsiveContainer>
        </>
      )}

      {/* Calibrate button */}
      <div style={{ marginTop: "1rem", display: "flex", alignItems: "center", gap: "1rem" }}>
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
      </div>
    </div>
  );
}
