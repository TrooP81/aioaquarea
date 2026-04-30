"use client";

import { useEffect, useState } from "react";

interface SettingMeta {
  value: string;
  type: string;
  description: string;
  options?: string[];
}

type SettingsMap = Record<string, SettingMeta>;

const SETTING_GROUPS = [
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
    keys: ["latitude", "longitude"],
  },
  {
    title: "Optimizer Constraints",
    description: "Temperature and scheduling boundaries",
    keys: ["tank_min_temp", "tank_max_temp", "comfort_temp_min", "comfort_temp_max", "dhw_ready_by_hours"],
  },
  {
    title: "Polling",
    description: "Data fetch intervals",
    keys: ["poll_interval_seconds"],
  },
];

export default function SettingsPage() {
  const [settings, setSettings] = useState<SettingsMap>({});
  const [editValues, setEditValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);

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
            {group.description}
          </p>

          <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
            {group.keys
              .filter((key) => settings[key] && shouldShowKey(group.title, key))
              .map((key) => {
                const meta = settings[key];
                return (
                  <div key={key} style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
                    <label
                      style={{
                        minWidth: "220px",
                        fontSize: "0.875rem",
                        color: "var(--text-muted)",
                      }}
                      title={meta.description}
                    >
                      {meta.description}
                    </label>

                    {meta.options ? (
                      <select
                        value={editValues[key] || ""}
                        onChange={(e) =>
                          setEditValues((prev) => ({ ...prev, [key]: e.target.value }))
                        }
                        style={{
                          flex: 1,
                          maxWidth: "300px",
                          background: "var(--bg)",
                          color: "var(--text)",
                          border: "1px solid var(--border)",
                          borderRadius: "0.375rem",
                          padding: "0.5rem 0.75rem",
                          fontSize: "0.875rem",
                        }}
                      >
                        {meta.options.map((opt) => (
                          <option key={opt} value={opt}>
                            {opt}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        type={meta.type === "secret" ? "password" : "text"}
                        value={editValues[key] || ""}
                        onChange={(e) =>
                          setEditValues((prev) => ({ ...prev, [key]: e.target.value }))
                        }
                        placeholder={meta.description}
                        style={{
                          flex: 1,
                          maxWidth: "300px",
                          background: "var(--bg)",
                          color: "var(--text)",
                          border: "1px solid var(--border)",
                          borderRadius: "0.375rem",
                          padding: "0.5rem 0.75rem",
                          fontSize: "0.875rem",
                        }}
                      />
                    )}
                  </div>
                );
              })}
          </div>
        </div>
      ))}

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
