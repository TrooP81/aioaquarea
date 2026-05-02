"use client";

import { useEffect, useState } from "react";

interface SettingEntry {
  value: string;
  type: string;
  description: string;
  options?: string[] | null;
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
  quiet_mode_start: ":00",
  quiet_mode_end: ":00",
  poll_interval_seconds: "s",
};

function formatValue(key: string, entry: SettingEntry): string {
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

  const suffix = UNIT_SUFFIXES[key] || "";
  return `${val}${suffix}`;
}

export function SettingsPanel() {
  const [settings, setSettings] = useState<Record<string, SettingEntry> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/settings")
      .then((r) => {
        if (!r.ok) throw new Error(`API error (${r.status})`);
        return r.json();
      })
      .then((data) => setSettings(data))
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
      <div className="settings-panel-header">
        <h2 className="chart-title" style={{ margin: 0 }}>Heat Pump Settings</h2>
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
                      {formatValue(key, entry)}
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
