"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { TestConnection } from "../../components/TestConnection";
import { ComfortSchedule } from "../../components/ComfortSchedule";
import { SmartThingsOAuth } from "../../components/SmartThingsOAuth";
import { SmartThingsSensorSelector } from "../../components/SmartThingsSensorSelector";
import { LogViewer } from "../../components/LogViewer";
import { ResetDataCard } from "../../components/ResetDataCard";
import { HeatCurveAdvice, HeatCurveValues } from "../../components/HeatCurveAdvice";
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
  label?: string;
  unit?: string;
  min?: number;
  max?: number;
  step?: number;
}

type SettingsMap = Record<string, SettingMeta>;

interface FieldRule {
  label?: string;
  unit?: string;
  min?: number;
  max?: number;
  step?: number;
  inputType?: "number" | "time" | "url" | "text";
}

const FIELD_RULES: Record<string, FieldRule> = {
  tank_min_temp: { label: "Minimum tank temperature during comfort hours", unit: "°C", min: 20, max: 65, step: 0.5, inputType: "number" },
  tank_min_temp_offpeak: { label: "Minimum tank temperature during off-peak hours", unit: "°C", min: 20, max: 65, step: 0.5, inputType: "number" },
  tank_max_temp: { label: "Maximum tank temperature", unit: "°C", min: 30, max: 65, step: 0.5, inputType: "number" },
  comfort_temp_min: { label: "Comfort band minimum", unit: "°C", min: 10, max: 30, step: 0.1, inputType: "number" },
  comfort_temp_target: { label: "Comfort target", unit: "°C", min: 10, max: 30, step: 0.1, inputType: "number" },
  comfort_temp_max: { label: "Comfort band maximum", unit: "°C", min: 10, max: 30, step: 0.1, inputType: "number" },
  heat_curve_outdoor_cold_c: { label: "Outdoor cold point", unit: "°C", min: -35, max: 15, step: 1, inputType: "number" },
  heat_curve_supply_cold_c: { label: "Supply temperature at cold point", unit: "°C", min: 20, max: 65, step: 1, inputType: "number" },
  heat_curve_outdoor_warm_c: { label: "Outdoor warm point", unit: "°C", min: -5, max: 30, step: 1, inputType: "number" },
  heat_curve_supply_warm_c: { label: "Supply temperature at warm point", unit: "°C", min: 20, max: 65, step: 1, inputType: "number" },
  heat_curve_heating_off_outdoor_c: { label: "Heating-off outdoor cutoff", unit: "°C", min: 5, max: 30, step: 0.5, inputType: "number" },
  heat_curve_delta_t_c: { label: "Controller ΔT", unit: "°C", min: 1, max: 15, step: 1, inputType: "number" },
  quiet_mode_start: { label: "Quiet mode starts", unit: "hour", min: 0, max: 23, step: 1, inputType: "number" },
  quiet_mode_end: { label: "Quiet mode ends", unit: "hour", min: 0, max: 23, step: 1, inputType: "number" },
  latitude: { label: "Latitude", unit: "°", min: -90, max: 90, step: 0.0001, inputType: "number" },
  longitude: { label: "Longitude", unit: "°", min: -180, max: 180, step: 0.0001, inputType: "number" },
  outdoor_temperature_weather_offset_c: { label: "Local weather adjustment", unit: "°C", min: -10, max: 10, step: 0.1, inputType: "number" },
  outdoor_temperature_weather_max_age_minutes: { label: "Weather fallback timeout", unit: "min", min: 30, max: 720, step: 30, inputType: "number" },
  operational_alert_webhook_url: { label: "Alert webhook URL", inputType: "url" },
  thermal_lag_minutes: { label: "Thermal response lag", unit: "min", min: 0, max: 360, step: 5, inputType: "number" },
  poll_interval_seconds: { label: "Polling interval", unit: "seconds", min: 30, max: 3600, step: 30, inputType: "number" },
};

const ADVANCED_OPTIMIZER_GROUPS = new Set([
  "Price Sensitivity",
  "Adaptive Learning",
  "Seasonal Learning",
  "Comfort Model",
]);

/** Slugify a section title into a DOM id for anchor navigation. */
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");

const SETTINGS_TABS = [
  {
    id: "optimizer",
    label: "Optimizer",
    description: "Planning rules, comfort targets, and automatic learning",
    groups: ["Optimizer Layer", "Optimizer Constraints", "Controller Heat Curve", "Quiet Mode", "Price Sensitivity", "Adaptive Learning", "Seasonal Learning", "Comfort Model", "Shower Mode"],
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
    groups: ["Display", "Operational Alerts"],
  },
  {
    id: "system",
    label: "System",
    description: "Release notes, diagnostics, logs, and data reset",
    groups: ["Manual trial suggestions"],
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
    description: "Choose the forecast provider and which outdoor temperature planning and ML should trust",
    keys: [
      "weather_provider",
      "outdoor_temperature_source",
      "outdoor_temperature_weather_offset_c",
      "outdoor_temperature_weather_max_age_minutes",
      "manual_outdoor_temp",
      "manual_wind_speed",
      "manual_humidity",
      "manual_irradiance",
      "manual_precipitation",
    ],
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
    title: "Controller Heat Curve",
    description: "Record the Panasonic values after changing them manually. The planners use this outdoor-to-supply-water curve; saving here never controls the heat pump.",
    keys: [
      "heat_curve_outdoor_cold_c",
      "heat_curve_supply_cold_c",
      "heat_curve_outdoor_warm_c",
      "heat_curve_supply_warm_c",
      "heat_curve_heating_off_outdoor_c",
      "heat_curve_delta_t_c",
    ],
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
    title: "Seasonal Learning",
    description: "Opt-in observe-only evidence collection during heating weather. It can train locally and end itself only after both demand and indoor-heating evidence are ready.",
    keys: ["seasonal_calibration_enabled", "seasonal_calibration_max_outdoor_c", "seasonal_calibration_window_days", "seasonal_calibration_auto_train", "seasonal_calibration_auto_exit"],
  },
  {
    title: "Polling",
    description: "Data fetch intervals",
    keys: ["poll_interval_seconds"],
  },
  {
    title: "SmartThings Integration",
    description: "Indoor temperature sensors via Samsung SmartThings",
    keys: ["smartthings_enabled", "smartthings_client_id", "smartthings_client_secret", "smartthings_redirect_uri", "smartthings_pat", "smartthings_device_ids", "comfort_reference_sensor_id", "smartthings_poll_interval"],
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
  {
    title: "Operational Alerts",
    description: "In-app warnings are available by default. Add an optional HTTPS webhook only if you want external notifications.",
    keys: ["operational_alerts_enabled", "operational_alert_webhook_url"],
  },
  {
    title: "Manual trial suggestions",
    description: "Optional, review-only heat-curve trials for measuring changes against similar weather. They never send heat-pump commands.",
    keys: ["outcome_experiments_enabled", "outcome_experiment_max_curve_step_c"],
  },
];

function tabForSetting(key: string): SettingsTabId | null {
  const group = SETTING_GROUPS.find((candidate) => candidate.keys.includes(key));
  if (!group) return null;
  return (SETTINGS_TABS.find((tab) => tab.groups.includes(group.title as never))?.id ?? null) as SettingsTabId | null;
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<SettingsMap>({});
  const [editValues, setEditValues] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);
  const [apiVersion, setApiVersion] = useState<string | null>(null);
  const [apiContract, setApiContract] = useState<string | null>(null);
  const [apiVersionUnavailable, setApiVersionUnavailable] = useState(false);
  const [activeTab, setActiveTab] = useState<SettingsTabId>("optimizer");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const currency = useCurrency();
  const activeTabMeta = SETTINGS_TABS.find((tab) => tab.id === activeTab) ?? SETTINGS_TABS[0];
  const visibleGroupTitles: readonly string[] = activeTabMeta.groups;
  const dirtyKeys = useMemo(
    () => Object.keys(editValues).filter((key) => {
      const original = settings[key]?.value ?? "";
      return editValues[key] !== original && !editValues[key].includes("***");
    }),
    [editValues, settings],
  );
  const validationErrors = useMemo(() => {
    const errors: Record<string, string> = {};
    for (const [key, value] of Object.entries(editValues)) {
      const meta = settings[key];
      if (!meta || value.includes("***")) continue;
      const rule = FIELD_RULES[key] ?? {};
      const numeric = rule.inputType === "number" || ["int", "float", "number"].includes(meta.type);
      if (numeric) {
        const parsed = Number(value);
        if (!Number.isFinite(parsed)) {
          errors[key] = "Enter a valid number.";
          continue;
        }
        const min = rule.min ?? meta.min;
        const max = rule.max ?? meta.max;
        if (min != null && parsed < min) errors[key] = `Minimum is ${min}${rule.unit ? ` ${rule.unit}` : ""}.`;
        if (max != null && parsed > max) errors[key] = `Maximum is ${max}${rule.unit ? ` ${rule.unit}` : ""}.`;
      }
      if (rule.inputType === "url" && value && !/^https:\/\//i.test(value)) {
        errors[key] = "Use an HTTPS URL.";
      }
    }
    const numberValue = (key: string) => Number(editValues[key]);
    if (Number.isFinite(numberValue("tank_min_temp")) && Number.isFinite(numberValue("tank_max_temp")) && numberValue("tank_min_temp") > numberValue("tank_max_temp")) {
      errors.tank_min_temp = "Must not exceed maximum tank temperature.";
    }
    if (Number.isFinite(numberValue("tank_min_temp_offpeak")) && Number.isFinite(numberValue("tank_max_temp")) && numberValue("tank_min_temp_offpeak") > numberValue("tank_max_temp")) {
      errors.tank_min_temp_offpeak = "Must not exceed maximum tank temperature.";
    }
    if (Number.isFinite(numberValue("comfort_temp_min")) && Number.isFinite(numberValue("comfort_temp_max")) && numberValue("comfort_temp_min") > numberValue("comfort_temp_max")) {
      errors.comfort_temp_min = "Minimum comfort temperature must be below the maximum.";
    }
    if (Number.isFinite(numberValue("comfort_temp_target"))) {
      if (numberValue("comfort_temp_target") < numberValue("comfort_temp_min") || numberValue("comfort_temp_target") > numberValue("comfort_temp_max")) {
        errors.comfort_temp_target = "Target must remain inside the configured comfort band.";
      }
    }
    if (Number.isFinite(numberValue("heat_curve_outdoor_cold_c")) && Number.isFinite(numberValue("heat_curve_outdoor_warm_c")) && numberValue("heat_curve_outdoor_cold_c") >= numberValue("heat_curve_outdoor_warm_c")) {
      errors.heat_curve_outdoor_cold_c = "Cold outdoor point must be below the warm point.";
    }
    return errors;
  }, [editValues, settings]);
  const activeTabDirtyCount = dirtyKeys.filter((key) => tabForSetting(key) === activeTab).length;

  useEffect(() => {
    fetchSettings();
    fetchApiVersion();
  }, []);

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirtyKeys.length === 0) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirtyKeys.length]);

  useEffect(() => {
    const applyLocation = () => {
      const requestedTab = new URLSearchParams(window.location.search).get("tab");
      if (SETTINGS_TABS.some((tab) => tab.id === requestedTab)) {
        setActiveTab(requestedTab as SettingsTabId);
      }
    };
    applyLocation();
    window.addEventListener("popstate", applyLocation);
    return () => window.removeEventListener("popstate", applyLocation);
  }, []);

  useEffect(() => {
    const targetId = window.location.hash.slice(1);
    const target = targetId ? document.getElementById(targetId) : null;
    if (target && !target.hidden) {
      target.scrollIntoView({ block: "start" });
    }
  }, [activeTab]);

  const fetchApiVersion = async () => {
    try {
      const res = await fetch("/api/version");
      if (!res.ok) throw new Error("Failed to load API version");
      const data: { version?: unknown; api_contract?: unknown } = await res.json();
      if (typeof data.version !== "string" || typeof data.api_contract !== "string") {
        throw new Error("Invalid API version response");
      }
      setApiVersion(data.version);
      setApiContract(data.api_contract);
      setApiVersionUnavailable(false);
    } catch {
      setApiVersion(null);
      setApiContract(null);
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
    } catch {
      setMessage({ text: "Failed to load settings", type: "error" });
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    if (Object.keys(validationErrors).length > 0) {
      setMessage({ text: `Fix ${Object.keys(validationErrors).length} invalid field(s) before saving.`, type: "error" });
      return;
    }
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

  const useHeatCurveSuggestion = (suggested: HeatCurveValues) => {
    setEditValues((previous) => ({
      ...previous,
      heat_curve_outdoor_cold_c: String(suggested.outdoor_cold_c),
      heat_curve_supply_cold_c: String(suggested.supply_cold_c),
      heat_curve_outdoor_warm_c: String(suggested.outdoor_warm_c),
      heat_curve_supply_warm_c: String(suggested.supply_warm_c),
      heat_curve_heating_off_outdoor_c: String(suggested.heating_off_outdoor_c),
      heat_curve_delta_t_c: String(suggested.delta_t_c),
    }));
    setMessage({ text: "Recommendation copied to the draft. Apply it manually on the controller, verify the fields, then save.", type: "success" });
  };

  const selectTab = (tab: SettingsTabId) => {
    setActiveTab(tab);
    const url = new URL(window.location.href);
    url.searchParams.set("tab", tab);
    window.history.pushState({ tab }, "", url);
  };

  const discardChanges = async () => {
    if (dirtyKeys.length > 0 && !window.confirm(`Discard ${dirtyKeys.length} unsaved change(s)?`)) return;
    await fetchSettings();
    setMessage({ text: "Unsaved changes discarded", type: "success" });
  };

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
      if (!isManualWeatherMode && ["manual_outdoor_temp", "manual_wind_speed", "manual_humidity", "manual_irradiance", "manual_precipitation"].includes(key)) return false;
      if (
        editValues["outdoor_temperature_source"] !== "weather"
        && ["outdoor_temperature_weather_offset_c", "outdoor_temperature_weather_max_age_minutes"].includes(key)
      ) return false;
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
            <Link href="/" className="btn">← Dashboard</Link>
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
          <Link href="/" className="btn">← Dashboard</Link>
        </div>
      </div>

      <TabNavigation
        activeId={activeTab}
        ariaLabel="Settings categories"
        idPrefix="settings"
        items={SETTINGS_TABS}
        onChange={selectTab}
      />

      <div className="tab-context" aria-live="polite">
        <strong>{activeTabMeta.label}</strong>
        <span>{activeTabMeta.description}</span>
        {activeTabDirtyCount > 0 && <span className="settings-dirty-badge">{activeTabDirtyCount} unsaved here</span>}
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

      <div className="settings-action-bar settings-action-bar--sticky">
        <div className="settings-save-summary" aria-live="polite">
          <strong>{dirtyKeys.length === 0 ? "No unsaved changes" : `${dirtyKeys.length} unsaved change${dirtyKeys.length === 1 ? "" : "s"}`}</strong>
          {dirtyKeys.length > 0 && (
            <span>
              {Array.from(new Set(dirtyKeys.map(tabForSetting).filter(Boolean))).map((tab) => SETTINGS_TABS.find((item) => item.id === tab)?.label).join(", ")}
            </span>
          )}
          {Object.keys(validationErrors).length > 0 && <span className="text-danger">{Object.keys(validationErrors).length} field(s) need attention</span>}
        </div>
        <button className="btn btn-primary" onClick={handleSave} disabled={saving || dirtyKeys.length === 0 || Object.keys(validationErrors).length > 0}>
          {saving ? "Saving..." : `Save ${dirtyKeys.length || ""} change${dirtyKeys.length === 1 ? "" : "s"}`}
        </button>
        <button className="btn" onClick={discardChanges} disabled={dirtyKeys.length === 0}>
          Discard draft
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
      {activeTab === "optimizer" && <HeatCurveAdvice onUseSuggestion={useHeatCurveSuggestion} />}
      {activeTab === "optimizer" && (
        <div className="settings-level-toggle">
          <div>
            <strong>{showAdvanced ? "Advanced optimizer settings" : "Essential optimizer settings"}</strong>
            <span>{showAdvanced ? "Includes model thresholds and seasonal-learning controls." : "Comfort, heat curve, quiet hours, and safe operating limits."}</span>
          </div>
          <button className="btn btn-sm" onClick={() => setShowAdvanced((value) => !value)}>
            {showAdvanced ? "Hide advanced" : "Show advanced"}
          </button>
        </div>
      )}
      {SETTING_GROUPS.filter((group) => activeTab !== "optimizer" || showAdvanced || !ADVANCED_OPTIMIZER_GROUPS.has(group.title)).map((group) => (
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
          {group.title === "Price Provider" && currency.warning && (
            <p className="text-warning text-sm">⚠ {currency.warning}</p>
          )}

          {group.title === "SmartThings Integration" && editValues["smartthings_enabled"] === "true" && (
            <SmartThingsOAuth />
          )}

          <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
            {group.keys
              .filter((key) => settings[key] && shouldShowKey(group.title, key))
              .map((key) => {
                const meta = settings[key];
                const rule = FIELD_RULES[key] ?? {};
                const description =
                  key === "manual_price_eur_per_kwh"
                    ? `Static electricity price (${currency.code}/kWh)`
                    : meta.description;
                const label = meta.label || rule.label || description;
                const unit = meta.unit || rule.unit;
                const inputType = rule.inputType
                  || (["int", "float", "number"].includes(meta.type) ? "number" : meta.type === "secret" ? "password" : "text");
                const isBooleanSetting = meta.type === "bool"
                  || Boolean(meta.options?.includes("true") && meta.options?.includes("false") && meta.options.length === 2);
                return (
                  <div key={key} className="settings-form-row">
                    <div className="settings-field-copy">
                      <label htmlFor={`setting-${key}`} className="settings-form-label">{label}</label>
                      {description !== label && <span className="settings-form-hint">{description}</span>}
                    </div>

                    {key === "smartthings_device_ids" ? (
                      <SmartThingsSensorSelector
                        value={editValues[key] || ""}
                        onChange={(next) =>
                          setEditValues((prev) => ({ ...prev, [key]: next }))
                        }
                      />
                    ) : key === "comfort_reference_sensor_id" ? (
                      <SmartThingsSensorSelector
                        value={editValues[key] || ""}
                        onChange={(next) =>
                          setEditValues((prev) => ({ ...prev, [key]: next }))
                        }
                        single
                        title="Reference room for comfort"
                        emptyMessage="No reference — use robust median of selected sensors"
                      />
                    ) : meta.options && !isBooleanSetting ? (
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
                              {key === "optimizer_layer"
                                ? (OPTIMIZER_LAYER_OPTIONS[opt]?.label || opt)
                                : key === "outdoor_temperature_source"
                                  ? opt === "weather"
                                    ? "Weather report (recommended)"
                                    : "Heat-pump sensor"
                                  : opt}
                            </option>
                          ))}
                        </select>
                        {key === "optimizer_layer" && OPTIMIZER_LAYER_OPTIONS[editValues[key]] && (
                          <span className="settings-form-hint">
                            {OPTIMIZER_LAYER_OPTIONS[editValues[key]].description}
                          </span>
                        )}
                      </>
                    ) : isBooleanSetting ? (
                      <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", minHeight: "2.5rem" }}>
                        <input
                          id={`setting-${key}`}
                          type="checkbox"
                          checked={editValues[key] === "true"}
                          onChange={(e) =>
                            setEditValues((prev) => ({ ...prev, [key]: e.target.checked ? "true" : "false" }))
                          }
                          style={{ width: "1.1rem", height: "1.1rem", accentColor: "var(--primary)" }}
                        />
                        <span className="text-muted text-sm">{editValues[key] === "true" ? "Enabled" : "Disabled"}</span>
                      </div>
                    ) : (
                      <div className="settings-input-wrap">
                        <input
                          id={`setting-${key}`}
                          type={inputType}
                          value={editValues[key] || ""}
                          onChange={(e) =>
                            setEditValues((prev) => ({ ...prev, [key]: e.target.value }))
                          }
                          min={rule.min ?? meta.min}
                          max={rule.max ?? meta.max}
                          step={rule.step ?? meta.step}
                          placeholder={meta.description}
                          className={`form-input ${validationErrors[key] ? "form-input--invalid" : ""}`}
                          aria-invalid={Boolean(validationErrors[key])}
                          aria-describedby={validationErrors[key] ? `setting-error-${key}` : undefined}
                        />
                        {unit && <span className="settings-input-unit">{unit}</span>}
                        {validationErrors[key] && <span id={`setting-error-${key}`} className="settings-field-error">{validationErrors[key]}</span>}
                      </div>
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
          <div className="release-runtime-item">
            <dt>Forecast API contract</dt>
            <dd data-testid="api-contract">
              {apiContract ?? (apiVersionUnavailable ? "Unavailable" : "Checking...")}
            </dd>
            <span>
              {apiContract ? "Unified forecast data" : apiVersionUnavailable ? "Could not verify" : "Checking running service"}
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
