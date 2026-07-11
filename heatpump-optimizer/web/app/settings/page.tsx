"use client";

import { useEffect, useState } from "react";
import { TestConnection } from "../../components/TestConnection";
import { ComfortSchedule } from "../../components/ComfortSchedule";
import { SmartThingsOAuth } from "../../components/SmartThingsOAuth";
import { SmartThingsSensorSelector } from "../../components/SmartThingsSensorSelector";
import { LogViewer } from "../../components/LogViewer";
import { ResetDataCard } from "../../components/ResetDataCard";
import { useCurrency } from "../../components/useCurrency";
import { AppVersionBadge } from "@/components/AppVersionBadge";
import { TabNavigation } from "@/components/TabNavigation";
import { OPTIMIZER_LAYER_OPTIONS } from "@/lib/constants";
import { APP_VERSION, RELEASE_HISTORY } from "@/lib/release";

interface SettingMeta {
  value: string;
  type: string;
  description: string;
  options?: string[];
}

type SettingsMap = Record<string, SettingMeta>;

/** Slugify a section title into a DOM id for anchor navigation. */
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");

const SETTINGS_TABS = [
  {
    id: "optimizer",
    label: "Optimizer",
    description: "Planning rules, comfort targets, and automatic learning",
    groups: ["Optimizer Layer", "Optimizer Constraints", "Quiet Mode", "Price Sensitivity", "Adaptive Learning", "Comfort Model", "Shower Mode"],
  },
  {
    id: "data",
    label: "Data Sources",
    description: "Electricity prices, weather, location, and polling",
    groups: ["Price Provider", "Weather Provider", "Location", "Polling"],
  },
  {
    id: "integrations",
    label: "Integrations",
    description: "Heat-pump and SmartThings connections",
    groups: ["Panasonic Aquarea", "SmartThings Integration"],
  },
  {
    id: "display",
    label: "Display",
    description: "Currency and time-format preferences",
    groups: ["Display"],
  },
  {
    id: "system",
    label: "System",
    description: "Release notes, diagnostics, logs, and data reset",
    groups: [],
  },
] as const;

type SettingsTabId = (typeof SETTINGS_TABS)[number]["id"];

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
  const [apiVersion, setApiVersion] = useState<string | null>(null);
  const [apiVersionUnavailable, setApiVersionUnavailable] = useState(false);
  const [activeTab, setActiveTab] = useState<SettingsTabId>("optimizer");
  const currency = useCurrency();
  const activeTabMeta = SETTINGS_TABS.find((tab) => tab.id === activeTab) ?? SETTINGS_TABS[0];
  const visibleGroupTitles: readonly string[] = activeTabMeta.groups;

  useEffect(() => {
    fetchSettings();
    fetchApiVersion();
  }, []);

  const fetchApiVersion = async () => {
    try {
      const res = await fetch("/api/version");
      if (!res.ok) throw new Error("Failed to load API version");
      const data: { version?: unknown } = await res.json();
      if (typeof data.version !== "string") throw new Error("Invalid API version response");
      setApiVersion(data.version);
      setApiVersionUnavailable(false);
    } catch {
      setApiVersion(null);
      setApiVersionUnavailable(true);
    }
  };

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
          <div className="header-actions">
            <AppVersionBadge />
            <a href="/" className="btn">← Dashboard</a>
          </div>
        </div>
        <p style={{ color: "var(--text-muted)" }}>Loading settings...</p>
      </div>
    );
  }

  return (
    <div className="dashboard">
      <div className="header">
        <h1>Settings</h1>
        <div className="header-actions">
          <AppVersionBadge />
          <a href="/" className="btn">← Dashboard</a>
        </div>
      </div>

      <TabNavigation
        activeId={activeTab}
        ariaLabel="Settings categories"
        idPrefix="settings"
        items={SETTINGS_TABS}
        onChange={setActiveTab}
      />

      <div className="tab-context" aria-live="polite">
        <strong>{activeTabMeta.label}</strong>
        <span>{activeTabMeta.description}</span>
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

      <div className="settings-action-bar">
        <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
          {saving ? "Saving..." : "Save Settings"}
        </button>
        <button className="btn" onClick={fetchSettings}>
          Reset form
        </button>
      </div>

      {SETTINGS_TABS.filter((tab) => tab.id !== activeTab).map((tab) => (
        <div
          key={tab.id}
          id={`settings-panel-${tab.id}`}
          role="tabpanel"
          aria-labelledby={`settings-tab-${tab.id}`}
          hidden
        />
      ))}

      <div
        id={`settings-panel-${activeTab}`}
        className="settings-tab-workspace"
        role="tabpanel"
        aria-labelledby={`settings-tab-${activeTab}`}
      >
      {SETTING_GROUPS.map((group) => (
        <div
          key={group.title}
          id={slug(group.title)}
          className="plan-section settings-tab-panel"
          hidden={!visibleGroupTitles.includes(group.title)}
        >
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

      <section id="release-history" className="plan-section release-history settings-tab-panel" hidden={activeTab !== "system"}>
        <div className="release-history-header">
          <div>
            <h2 className="chart-title">Release History</h2>
            <p className="release-history-intro">
              Dashboard version <strong>v{APP_VERSION}</strong> is live in the interface you are viewing.
            </p>
          </div>
          <span className="status-badge online">● Live now</span>
        </div>

        <dl className="release-runtime" aria-label="Running service versions">
          <div className="release-runtime-item">
            <dt>Web dashboard</dt>
            <dd data-testid="dashboard-version">v{APP_VERSION}</dd>
            <span>Live build</span>
          </div>
          <div className="release-runtime-item">
            <dt>API service</dt>
            <dd data-testid="api-version">
              {apiVersion ? `v${apiVersion}` : apiVersionUnavailable ? "Unavailable" : "Checking..."}
            </dd>
            <span className={apiVersion && apiVersion !== APP_VERSION ? "release-version-warning" : undefined}>
              {apiVersion
                ? apiVersion === APP_VERSION
                  ? "Matches dashboard"
                  : "Different from dashboard"
                : apiVersionUnavailable
                  ? "Could not verify"
                  : "Checking running service"}
            </span>
          </div>
        </dl>

        <ol className="release-list">
          {RELEASE_HISTORY.map((release, index) => (
            <li key={release.version} className="release-list-item">
              <div className="release-list-heading">
                <span className="release-version">v{release.version}</span>
                {index === 0 && <span className="release-current">Current</span>}
                <span className="release-date">{release.released}</span>
              </div>
              <h3>{release.title}</h3>
              <ul>
                {release.changes.map((change) => (
                  <li key={change}>{change}</li>
                ))}
              </ul>
            </li>
          ))}
        </ol>
      </section>

      <div id="comfort-schedule" className="settings-tab-panel" hidden={activeTab !== "optimizer"}>
        <ComfortSchedule />
      </div>

      <div id="test-connection" className="settings-tab-panel" hidden={activeTab !== "system"}>
        <TestConnection editValues={editValues} />
      </div>

      <div id="logs" className="settings-tab-panel" hidden={activeTab !== "system"}>
        <LogViewer />
      </div>

      <div id="reset-data" className="settings-tab-panel" hidden={activeTab !== "system"}>
        <ResetDataCard />
      </div>
      </div>
    </div>
  );
}
