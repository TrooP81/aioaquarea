"use client";

import { useEffect, useState } from "react";
import { TestConnection } from "../../components/TestConnection";
import { ComfortSchedule } from "../../components/ComfortSchedule";
import { SmartThingsOAuth } from "../../components/SmartThingsOAuth";
import { SmartThingsSensorSelector } from "../../components/SmartThingsSensorSelector";
import { LogViewer } from "../../components/LogViewer";
import { useCurrency } from "../../components/useCurrency";
import { OPTIMIZER_LAYER_OPTIONS } from "@/lib/constants";

interface SettingMeta {
  value: string;
  type: string;
  description: string;
  options?: string[];
}

type SettingsMap = Record<string, SettingMeta>;

const SETTING_GROUPS = [
  {
    title: "Optimizer Layer",
    description: "Choose which optimization engine drives scheduling",
    keys: ["optimizer_layer"],
  },
  {
    title: "Price Provider",
    description: "Configure how electricity prices are fetched",
    keys: ["price_provider", "entsoe_api_token", "entsoe_area", "tibber_api_token", "manual_price_eur_per_kwh"],
  },
  {
    title: "Weather Provider",
    description: "Configure how weather data is fetched",
    keys: ["weather_provider", "manual_outdoor_temp", "manual_wind_speed", "manual_humidity", "manual_irradiance"],
  },
  {
    title: "Panasonic Aquarea",
    description: "Heat pump API credentials",
    keys: ["aquarea_username", "aquarea_password"],
  },
  {
    title: "Location",
    description: "Coordinates for weather and price area lookup",
    keys: ["latitude", "longitude", "timezone"],
  },
  {
    title: "Optimizer Constraints",
    description: "Temperature and scheduling boundaries",
    keys: ["tank_min_temp", "tank_min_temp_offpeak", "tank_max_temp", "comfort_temp_min", "comfort_temp_max"],
  },
  {
    title: "Quiet Mode",
    description: "Compressor noise reduction schedule",
    keys: ["quiet_mode_start", "quiet_mode_end"],
  },
  {
    title: "Price Sensitivity",
    description: "How aggressively the optimizer reacts to electricity prices",
    keys: ["price_comfort_override_pct", "price_eco_upgrade_pct"],
  },
  {
    title: "Adaptive Learning",
    description: "Automatic schedule adjustment from observed usage",
    keys: ["learned_schedule_threshold"],
  },
  {
    title: "Polling",
    description: "Data fetch intervals",
    keys: ["poll_interval_seconds"],
  },
  {
    title: "SmartThings Integration",
    description: "Indoor temperature sensors via Samsung SmartThings",
    keys: ["smartthings_enabled", "smartthings_client_id", "smartthings_client_secret", "smartthings_redirect_uri", "smartthings_pat", "smartthings_device_ids", "smartthings_poll_interval"],
  },
  {
    title: "Comfort Model",
    description: "ML model that learns indoor temperature from water supply temp",
    keys: ["use_comfort_model", "comfort_temp_target", "thermal_lag_minutes"],
  },
  {
    title: "Shower Mode",
    description: "Reactive DHW boost when a rapid tank temperature drop is detected (e.g. during a shower)",
    keys: ["shower_mode_enabled", "shower_drop_threshold", "shower_max_duration_minutes"],
  },
  {
    title: "Display",
    description: "Currency and display preferences",
    keys: ["currency", "time_format"],
  },
];

export default function SettingsPage() {
  const [settings, setSettings] = useState<SettingsMap>({});
  const [editValues, setEditValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);
  const currency = useCurrency();

  useEffect(() => {
    fetchSettings();
  }, []);

  const fetchSettings = async () => {
    try {
      const res = await fetch("/api/settings");
      if (!res.ok) throw new Error("Failed to load settings");
      const data: SettingsMap = await res.json();
      setSettings(data);
      // Initialize edit values with current values
      const vals: Record<string, string> = {};
      for (const [key, meta] of Object.entries(data)) {
        vals[key] = meta.value;
      }
      setEditValues(vals);
    } catch (e) {
      setMessage({ text: "Failed to load settings", type: "error" });
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setMessage(null);

    // Only send changed values (and skip masked secrets that weren't edited)
    const updates: Record<string, string> = {};
    for (const [key, val] of Object.entries(editValues)) {
      const original = settings[key]?.value || "";
      if (val !== original && !val.includes("***")) {
        updates[key] = val;
      }
    }

    if (Object.keys(updates).length === 0) {
      setMessage({ text: "No changes to save", type: "success" });
      setSaving(false);
      return;
    }

    try {
      const res = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: updates }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Save failed");
      }
      setMessage({ text: `Saved ${Object.keys(updates).length} setting(s)`, type: "success" });
      await fetchSettings();
    } catch (e) {
      setMessage({ text: e instanceof Error ? e.message : "Save failed", type: "error" });
    } finally {
      setSaving(false);
    }
  };

  const isManualPriceMode = editValues["price_provider"] === "manual";
  const isManualWeatherMode = editValues["weather_provider"] === "manual";

  const shouldShowKey = (groupTitle: string, key: string): boolean => {
    // Hide API-specific fields when in manual mode
    if (groupTitle === "Price Provider") {
      if (isManualPriceMode && ["entsoe_api_token", "entsoe_area", "tibber_api_token"].includes(key)) return false;
      if (!isManualPriceMode && key === "manual_price_eur_per_kwh") return false;
      if (editValues["price_provider"] !== "tibber" && key === "tibber_api_token") return false;
      if (editValues["price_provider"] !== "entsoe" && ["entsoe_api_token", "entsoe_area"].includes(key)) return false;
    }
    if (groupTitle === "Weather Provider") {
      if (isManualWeatherMode && false) return false; // show manual fields
      if (!isManualWeatherMode && ["manual_outdoor_temp", "manual_wind_speed", "manual_humidity", "manual_irradiance"].includes(key)) return false;
    }
    if (groupTitle === "SmartThings Integration") {
      if (editValues["smartthings_enabled"] !== "true" && key !== "smartthings_enabled") return false;
    }
    if (groupTitle === "Comfort Model") {
      if (editValues["use_comfort_model"] !== "true" && key !== "use_comfort_model") return false;
    }
    return true;
  };

  if (loading) {
    return (
      <div className="dashboard">
        <div className="header">
          <h1>Settings</h1>
          <a href="/" className="btn">← Dashboard</a>
        </div>
        <p style={{ color: "var(--text-muted)" }}>Loading settings...</p>
      </div>
    );
  }

  return (
    <div className="dashboard">
      <div className="header">
        <h1>Settings</h1>
        <a href="/" className="btn">← Dashboard</a>
      </div>

      {message && (
        <div
          className="override-banner"
          style={{
            borderColor: message.type === "success" ? "var(--success)" : "var(--danger)",
            background: message.type === "success" ? "rgba(34,197,94,0.1)" : "rgba(239,68,68,0.1)",
          }}
        >
          <p style={{ color: message.type === "success" ? "var(--success)" : "var(--danger)" }}>
            {message.text}
          </p>
        </div>
      )}

      {SETTING_GROUPS.map((group) => (
        <div key={group.title} className="plan-section">
          <h2 className="chart-title">{group.title}</h2>
          <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", marginBottom: "1rem" }}>
            {group.title === "Price Provider"
              ? `Configure how electricity prices are fetched (displaying in ${currency.code})`
              : group.description}
          </p>

          {group.title === "SmartThings Integration" && editValues["smartthings_enabled"] === "true" && (
            <SmartThingsOAuth />
          )}

          <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
            {group.keys
              .filter((key) => settings[key] && shouldShowKey(group.title, key))
              .map((key) => {
                const meta = settings[key];
                const description =
                  key === "manual_price_eur_per_kwh"
                    ? `Static electricity price (${currency.code}/kWh)`
                    : meta.description;
                return (
                  <div key={key} className="settings-form-row">
                    <label
                      htmlFor={`setting-${key}`}
                      className="settings-form-label"
                      title={description}
                    >
                      {description}
                    </label>

                    {key === "smartthings_device_ids" ? (
                      <SmartThingsSensorSelector
                        value={editValues[key] || ""}
                        onChange={(next) =>
                          setEditValues((prev) => ({ ...prev, [key]: next }))
                        }
                      />
                    ) : meta.options ? (
                      <>
                        <select
                          id={`setting-${key}`}
                          value={editValues[key] || ""}
                          onChange={(e) =>
                            setEditValues((prev) => ({ ...prev, [key]: e.target.value }))
                          }
                          className="form-select"
                        >
                          {meta.options.map((opt) => (
                            <option key={opt} value={opt}>
                              {key === "optimizer_layer" ? (OPTIMIZER_LAYER_OPTIONS[opt]?.label || opt) : opt}
                            </option>
                          ))}
                        </select>
                        {key === "optimizer_layer" && OPTIMIZER_LAYER_OPTIONS[editValues[key]] && (
                          <span className="settings-form-hint">
                            {OPTIMIZER_LAYER_OPTIONS[editValues[key]].description}
                          </span>
                        )}
                      </>
                    ) : (
                      <input
                        id={`setting-${key}`}
                        type={meta.type === "secret" ? "password" : "text"}
                        value={editValues[key] || ""}
                        onChange={(e) =>
                          setEditValues((prev) => ({ ...prev, [key]: e.target.value }))
                        }
                        placeholder={meta.description}
                        className="form-input"
                      />
                    )}
                  </div>
                );
              })}
          </div>
        </div>
      ))}

      <ComfortSchedule />

      <TestConnection editValues={editValues} />

      <LogViewer />

      <div style={{ display: "flex", gap: "1rem", marginTop: "1rem" }}>
        <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
          {saving ? "Saving..." : "Save Settings"}
        </button>
        <button className="btn" onClick={fetchSettings}>
          Reset
        </button>
      </div>
    </div>
  );
}
