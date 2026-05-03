"use client";

import { useEffect, useState } from "react";
import { useTimeFormat, formatHourLabel } from "./useTimeFormat";

interface SettingEntry {
  value: string;
  type: string;
  description: string;
  options?: string[] | null;
}

interface DeviceSettings {
  polled_at: string;
  device_id: string;
  mode: string | null;
  operation_status: number | null;
  tank_temp: number | null;
  tank_target_temp: number | null;
  tank_heat_max: number | null;
  tank_heat_min: number | null;
  tank_operation_status: number | null;
  zone1_temp: number | null;
  zone1_target_temp: number | null;
  zone1_operation_status: number | null;
  zone2_temp: number | null;
  zone2_target_temp: number | null;
  zone2_operation_status: number | null;
  quiet_mode: number | null;
  powerful_mode: number | null;
  special_status: number | null;
  force_dhw: number | null;
  force_heater: number | null;
  holiday_mode: number | null;
  outdoor_temp: number | null;
  direction: string | null;
  device_action: string | null;
  defrost_active: boolean | null;
  pump_duty: number | null;
}

/* Settings to show on the dashboard, grouped for readability.
   Keys not listed here are hidden (secrets, internal, etc). */
const GROUPS: { title: string; keys: string[] }[] = [
  {
    title: "Optimizer",
    keys: ["optimizer_layer", "price_provider", "weather_provider"],
  },
  {
    title: "Temperature Limits",
    keys: ["tank_min_temp", "tank_max_temp", "comfort_temp_min", "comfort_temp_max", "comfort_temp_target"],
  },
  {
    title: "Price Sensitivity",
    keys: ["manual_price_eur_per_kwh", "price_comfort_override_pct", "price_eco_upgrade_pct", "currency"],
  },
  {
    title: "Quiet Mode",
    keys: ["quiet_mode_start", "quiet_mode_end"],
  },
  {
    title: "Polling & Integrations",
    keys: ["poll_interval_seconds", "smartthings_enabled", "use_comfort_model"],
  },
];

const DISPLAY_LABELS: Record<string, string> = {
  optimizer_layer: "Optimizer mode",
  price_provider: "Price source",
  weather_provider: "Weather source",
  tank_min_temp: "Tank min",
  tank_max_temp: "Tank max",
  comfort_temp_min: "Comfort min",
  comfort_temp_max: "Comfort max",
  comfort_temp_target: "Comfort target",
  manual_price_eur_per_kwh: "Manual price",
  price_comfort_override_pct: "Peak override",
  price_eco_upgrade_pct: "Cheap upgrade",
  currency: "Currency",
  quiet_mode_start: "Start hour",
  quiet_mode_end: "End hour",
  poll_interval_seconds: "Poll interval",
  smartthings_enabled: "SmartThings",
  use_comfort_model: "ML comfort model",
};

const UNIT_SUFFIXES: Record<string, string> = {
  tank_min_temp: "°C",
  tank_max_temp: "°C",
  comfort_temp_min: "°C",
  comfort_temp_max: "°C",
  comfort_temp_target: "°C",
  manual_price_eur_per_kwh: " /kWh",
  price_comfort_override_pct: "th pctl",
  price_eco_upgrade_pct: "th pctl",
  poll_interval_seconds: "s",
};

function formatValue(key: string, entry: SettingEntry, hour12: boolean): string {
  const val = entry.value;
  if (val === "" || val === undefined || val === null) return "—";

  // Boolean-style
  if (val === "true") return "Enabled";
  if (val === "false") return "Disabled";

  // Readable option labels
  const optionLabels: Record<string, string> = {
    rules_only: "Rules only",
    milp_preferred: "MILP preferred",
    auto: "Auto",
    entsoe: "ENTSO-E",
    tibber: "Tibber",
    manual: "Manual",
    "open-meteo": "Open-Meteo",
  };
  if (optionLabels[val]) return optionLabels[val];

  // Quiet mode hours — format as time
  if (key === "quiet_mode_start" || key === "quiet_mode_end") {
    const h = parseInt(val, 10);
    if (!isNaN(h)) return formatHourLabel(h, hour12) + ":00";
  }

  const suffix = UNIT_SUFFIXES[key] || "";
  return `${val}${suffix}`;
}

/* --- Device settings humanisation --- */

const MODE_LABELS: Record<string, string> = {
  HEAT: "Heating",
  COOL: "Cooling",
  AUTO: "Auto",
  AUTO_HEAT: "Auto (heat)",
  AUTO_COOL: "Auto (cool)",
  OFF: "Off",
};

const ACTION_LABELS: Record<string, string> = {
  OFF: "Off",
  IDLE: "Idle",
  HEATING: "Heating",
  COOLING: "Cooling",
  HEATING_WATER: "Heating water",
};

const QUIET_LABELS: Record<number, string> = {
  0: "Off",
  1: "Level 1",
  2: "Level 2",
  3: "Level 3",
};

const POWERFUL_LABELS: Record<number, string> = {
  0: "Off",
  1: "30 min",
  2: "60 min",
  3: "90 min",
};

const SPECIAL_LABELS: Record<number, string> = {
  0: "None",
  1: "Eco",
  2: "Comfort",
};

function onOff(v: number | null): string {
  if (v === null || v === undefined) return "—";
  return v ? "On" : "Off";
}

function temp(v: number | null): string {
  if (v === null || v === undefined) return "—";
  return `${v}°C`;
}

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.round(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m ago`;
}

interface DeviceRow { label: string; value: string }

function buildDeviceRows(d: DeviceSettings): { title: string; rows: DeviceRow[] }[] {
  const groups: { title: string; rows: DeviceRow[] }[] = [];

  // General
  groups.push({
    title: "Unit Status",
    rows: [
      { label: "Mode", value: MODE_LABELS[d.mode ?? ""] ?? d.mode ?? "—" },
      { label: "Power", value: d.operation_status === 1 ? "On" : d.operation_status === 0 ? "Off" : "—" },
      { label: "Activity", value: ACTION_LABELS[d.device_action ?? ""] ?? d.device_action ?? "—" },
      { label: "Outdoor temp", value: temp(d.outdoor_temp) },
      { label: "Defrost", value: d.defrost_active === true ? "Active" : d.defrost_active === false ? "No" : "—" },
      { label: "Compressor", value: d.pump_duty === 1 ? "Running" : d.pump_duty === 0 ? "Off" : "—" },
    ],
  });

  // Tank (DHW)
  groups.push({
    title: "Hot Water Tank",
    rows: [
      { label: "Temperature", value: temp(d.tank_temp) },
      { label: "Target", value: temp(d.tank_target_temp) },
      { label: "Range", value: d.tank_heat_min != null && d.tank_heat_max != null ? `${d.tank_heat_min}–${d.tank_heat_max}°C` : "—" },
      { label: "Tank heating", value: onOff(d.tank_operation_status) },
      { label: "Force DHW", value: onOff(d.force_dhw) },
    ],
  });

  // Zone 1
  const z1Rows: DeviceRow[] = [
    { label: "Temperature", value: temp(d.zone1_temp) },
    { label: "Target", value: temp(d.zone1_target_temp) },
    { label: "Active", value: onOff(d.zone1_operation_status) },
  ];
  groups.push({ title: "Zone 1", rows: z1Rows });

  // Zone 2 — only show if any data exists
  if (d.zone2_temp != null || d.zone2_target_temp != null || d.zone2_operation_status != null) {
    groups.push({
      title: "Zone 2",
      rows: [
        { label: "Temperature", value: temp(d.zone2_temp) },
        { label: "Target", value: temp(d.zone2_target_temp) },
        { label: "Active", value: onOff(d.zone2_operation_status) },
      ],
    });
  }

  // Modes & Features
  groups.push({
    title: "Modes & Features",
    rows: [
      { label: "Quiet mode", value: QUIET_LABELS[d.quiet_mode ?? -1] ?? "—" },
      { label: "Powerful mode", value: POWERFUL_LABELS[d.powerful_mode ?? -1] ?? "—" },
      { label: "Special mode", value: SPECIAL_LABELS[d.special_status ?? -1] ?? "—" },
      { label: "Force heater", value: onOff(d.force_heater) },
      { label: "Holiday mode", value: onOff(d.holiday_mode) },
    ],
  });

  return groups;
}

export function SettingsPanel() {
  const [settings, setSettings] = useState<Record<string, SettingEntry> | null>(null);
  const [device, setDevice] = useState<DeviceSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timeFormat = useTimeFormat();

  useEffect(() => {
    Promise.all([
      fetch("/api/settings").then((r) => {
        if (!r.ok) throw new Error(`Settings API error (${r.status})`);
        return r.json();
      }),
      fetch("/api/device/settings").then((r) => {
        if (!r.ok) return null; // device data may not exist yet
        return r.json();
      }).catch(() => null),
    ])
      .then(([settingsData, deviceData]) => {
        setSettings(settingsData);
        setDevice(deviceData);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load settings"))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="plan-section">
        <h2 className="chart-title">Heat Pump Settings</h2>
        <div className="plan-loading">
          <div className="plan-loading-skeleton" />
          <div className="plan-loading-skeleton" style={{ width: "60%" }} />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="plan-section">
        <h2 className="chart-title">Heat Pump Settings</h2>
        <div className="plan-error"><span>{error}</span></div>
      </div>
    );
  }

  if (!settings) return null;

  return (
    <div className="plan-section">
      {/* Device settings from the heat pump */}
      {device && (
        <>
          <div className="settings-panel-header">
            <h2 className="chart-title" style={{ margin: 0 }}>Heat Pump Status</h2>
            <span className="settings-polled-at">Polled {relativeTime(device.polled_at)}</span>
          </div>

          <div className="settings-grid">
            {buildDeviceRows(device).map((group) => (
              <div key={group.title} className="settings-group">
                <h3 className="settings-group-title">{group.title}</h3>
                {group.rows.map((row) => (
                  <div key={row.label} className="settings-row">
                    <span className="settings-label">{row.label}</span>
                    <span className="settings-value">{row.value}</span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </>
      )}

      {/* Optimizer configuration settings */}
      <div className="settings-panel-header" style={device ? { marginTop: "1.5rem" } : undefined}>
        <h2 className="chart-title" style={{ margin: 0 }}>Optimizer Settings</h2>
        <a href="/settings" className="btn" style={{ fontSize: "0.8rem", padding: "0.3rem 0.75rem" }}>
          Edit
        </a>
      </div>

      <div className="settings-grid">
        {GROUPS.map((group) => {
          const visibleKeys = group.keys.filter((k) => settings[k]);
          if (visibleKeys.length === 0) return null;
          return (
            <div key={group.title} className="settings-group">
              <h3 className="settings-group-title">{group.title}</h3>
              {visibleKeys.map((key) => {
                const entry = settings[key];
                return (
                  <div key={key} className="settings-row">
                    <span className="settings-label" title={entry.description}>
                      {DISPLAY_LABELS[key] || key}
                    </span>
                    <span className="settings-value">
                      {formatValue(key, entry, timeFormat.hour12)}
                    </span>
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
}
