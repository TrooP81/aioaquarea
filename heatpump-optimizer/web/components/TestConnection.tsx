"use client";

import { useState } from "react";

interface TestResult {
  service: string;
  success: boolean;
  message: string;
  details?: Record<string, unknown>;
}

interface TestConnectionProps {
  /** Current edit values from the settings form (unmasked values entered by user) */
  editValues: Record<string, string>;
}

export function TestConnection({ editValues }: TestConnectionProps) {
  const [results, setResults] = useState<Record<string, TestResult>>({});
  const [testing, setTesting] = useState<Record<string, boolean>>({});

  const testService = async (service: string) => {
    setTesting((prev) => ({ ...prev, [service]: true }));
    setResults((prev) => {
      const copy = { ...prev };
      delete copy[service];
      return copy;
    });

    const body: Record<string, string> = { service };

    if (service === "aquarea") {
      const username = editValues["aquarea_username"] || "";
      const password = editValues["aquarea_password"] || "";
      // Only send credentials if they don't contain masked values
      if (username && !username.includes("***")) body.username = username;
      if (password && !password.includes("***")) body.password = password;
    } else if (service === "entsoe") {
      const token = editValues["entsoe_api_token"] || "";
      const area = editValues["entsoe_area"] || "";
      if (token && !token.includes("***")) body.api_token = token;
      if (area) body.area = area;
    } else if (service === "tibber") {
      const token = editValues["tibber_api_token"] || "";
      if (token && !token.includes("***")) body.api_token = token;
    } else if (service === "smartthings") {
      const pat = editValues["smartthings_pat"] || "";
      if (pat && !pat.includes("***")) body.pat = pat;
    }

    try {
      const res = await fetch("/api/test-connection", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data: TestResult = await res.json();
      setResults((prev) => ({ ...prev, [service]: data }));
    } catch {
      setResults((prev) => ({
        ...prev,
        [service]: {
          service,
          success: false,
          message: "Network error — is the API server running?",
        },
      }));
    } finally {
      setTesting((prev) => ({ ...prev, [service]: false }));
    }
  };

  const testAll = async () => {
    const services = getActiveServices();
    for (const svc of services) {
      await testService(svc);
    }
  };

  const getActiveServices = (): string[] => {
    const services: string[] = ["aquarea"];
    const priceProvider = editValues["price_provider"] || "";
    if (priceProvider === "entsoe") services.push("entsoe");
    if (priceProvider === "tibber") services.push("tibber");
    if (editValues["smartthings_enabled"] === "true") services.push("smartthings");
    return services;
  };

  const getStatusIcon = (service: string): string => {
    if (testing[service]) return "⏳";
    const result = results[service];
    if (!result) return "○";
    return result.success ? "✓" : "✗";
  };

  const getStatusColor = (service: string): string => {
    if (testing[service]) return "var(--text-muted)";
    const result = results[service];
    if (!result) return "var(--text-muted)";
    return result.success ? "var(--success)" : "var(--danger)";
  };

  const priceProvider = editValues["price_provider"] || "";

  return (
    <div className="plan-section">
      <h2 className="chart-title">Connection Tests</h2>
      <p style={{ color: "var(--text-muted)", fontSize: "0.875rem", marginBottom: "1rem" }}>
        Validate that your credentials and API tokens work before saving.
      </p>

      <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
        {/* Aquarea test */}
        <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
          <span
            style={{
              color: getStatusColor("aquarea"),
              fontWeight: 600,
              fontSize: "1.1rem",
              width: "1.5rem",
              textAlign: "center",
            }}
          >
            {getStatusIcon("aquarea")}
          </span>
          <span style={{ minWidth: "140px", fontSize: "0.875rem" }}>
            Panasonic Aquarea
          </span>
          <button
            className="btn"
            onClick={() => testService("aquarea")}
            disabled={testing["aquarea"]}
            style={{ fontSize: "0.8rem", padding: "0.25rem 0.75rem" }}
          >
            {testing["aquarea"] ? "Testing..." : "Test"}
          </button>
          {results["aquarea"] && (
            <span
              style={{
                fontSize: "0.8rem",
                color: results["aquarea"].success ? "var(--success)" : "var(--danger)",
              }}
            >
              {results["aquarea"].message}
            </span>
          )}
        </div>

        {/* ENTSO-E test (only if price_provider = entsoe) */}
        {priceProvider === "entsoe" && (
          <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
            <span
              style={{
                color: getStatusColor("entsoe"),
                fontWeight: 600,
                fontSize: "1.1rem",
                width: "1.5rem",
                textAlign: "center",
              }}
            >
              {getStatusIcon("entsoe")}
            </span>
            <span style={{ minWidth: "140px", fontSize: "0.875rem" }}>
              ENTSO-E Prices
            </span>
            <button
              className="btn"
              onClick={() => testService("entsoe")}
              disabled={testing["entsoe"]}
              style={{ fontSize: "0.8rem", padding: "0.25rem 0.75rem" }}
            >
              {testing["entsoe"] ? "Testing..." : "Test"}
            </button>
            {results["entsoe"] && (
              <span
                style={{
                  fontSize: "0.8rem",
                  color: results["entsoe"].success ? "var(--success)" : "var(--danger)",
                }}
              >
                {results["entsoe"].message}
              </span>
            )}
          </div>
        )}

        {/* Tibber test (only if price_provider = tibber) */}
        {priceProvider === "tibber" && (
          <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
            <span
              style={{
                color: getStatusColor("tibber"),
                fontWeight: 600,
                fontSize: "1.1rem",
                width: "1.5rem",
                textAlign: "center",
              }}
            >
              {getStatusIcon("tibber")}
            </span>
            <span style={{ minWidth: "140px", fontSize: "0.875rem" }}>
              Tibber API
            </span>
            <button
              className="btn"
              onClick={() => testService("tibber")}
              disabled={testing["tibber"]}
              style={{ fontSize: "0.8rem", padding: "0.25rem 0.75rem" }}
            >
              {testing["tibber"] ? "Testing..." : "Test"}
            </button>
            {results["tibber"] && (
              <span
                style={{
                  fontSize: "0.8rem",
                  color: results["tibber"].success ? "var(--success)" : "var(--danger)",
                }}
              >
                {results["tibber"].message}
              </span>
            )}
          </div>
        )}

        {/* SmartThings test (only if enabled) */}
        {editValues["smartthings_enabled"] === "true" && (
          <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
            <span
              style={{
                color: getStatusColor("smartthings"),
                fontWeight: 600,
                fontSize: "1.1rem",
                width: "1.5rem",
                textAlign: "center",
              }}
            >
              {getStatusIcon("smartthings")}
            </span>
            <span style={{ minWidth: "140px", fontSize: "0.875rem" }}>
              SmartThings
            </span>
            <button
              className="btn"
              onClick={() => testService("smartthings")}
              disabled={testing["smartthings"]}
              style={{ fontSize: "0.8rem", padding: "0.25rem 0.75rem" }}
            >
              {testing["smartthings"] ? "Testing..." : "Test"}
            </button>
            {results["smartthings"] && (
              <span
                style={{
                  fontSize: "0.8rem",
                  color: results["smartthings"].success ? "var(--success)" : "var(--danger)",
                }}
              >
                {results["smartthings"].message}
              </span>
            )}
          </div>
        )}
      </div>

      <div style={{ marginTop: "1rem" }}>
        <button
          className="btn btn-primary"
          onClick={testAll}
          disabled={Object.values(testing).some(Boolean)}
          style={{ fontSize: "0.8rem" }}
        >
          {Object.values(testing).some(Boolean) ? "Testing..." : "Test All Connections"}
        </button>
      </div>
    </div>
  );
}
