"use client";

import { useEffect, useState } from "react";

interface OAuthStatus {
  connected: boolean;
  method: "oauth" | "pat" | null;
  expires_at: string | null;
  scope?: string;
}

export function SmartThingsOAuth() {
  const [status, setStatus] = useState<OAuthStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchStatus = async () => {
    try {
      const res = await fetch("/api/smartthings/oauth/status");
      if (res.ok) {
        setStatus(await res.json());
      }
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchStatus();
    // Check URL params for OAuth callback result
    const params = new URLSearchParams(window.location.search);
    if (params.get("smartthings_oauth") === "connected") {
      // Remove the query param without reload
      window.history.replaceState({}, "", "/settings");
      fetchStatus();
    }
  }, []);

  const handleConnect = async () => {
    setConnecting(true);
    setError(null);
    try {
      const res = await fetch("/api/smartthings/oauth/authorize");
      if (!res.ok) {
        const data = await res.json();
        setError(data.detail || "Failed to start OAuth flow");
        return;
      }
      const { authorize_url } = await res.json();
      window.location.href = authorize_url;
    } catch (e) {
      setError("Failed to start OAuth flow");
    } finally {
      setConnecting(false);
    }
  };

  const handleDisconnect = async () => {
    if (!confirm("Disconnect SmartThings OAuth? You can reconnect anytime.")) return;
    setDisconnecting(true);
    try {
      await fetch("/api/smartthings/oauth/disconnect", { method: "DELETE" });
      await fetchStatus();
    } catch {
      setError("Failed to disconnect");
    } finally {
      setDisconnecting(false);
    }
  };

  if (loading) return null;

  return (
    <div
      style={{
        padding: "0.75rem 1rem",
        border: "1px solid var(--border)",
        borderRadius: "0.5rem",
        marginTop: "0.5rem",
        marginBottom: "0.5rem",
        background: "var(--surface)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "1rem", flexWrap: "wrap" }}>
        <span style={{ fontSize: "0.875rem", color: "var(--text-muted)" }}>
          OAuth Connection:
        </span>

        {status?.connected && status.method === "oauth" ? (
          <>
            <span style={{ color: "var(--success)", fontSize: "0.875rem", fontWeight: 500 }}>
              ● Connected via OAuth
            </span>
            {status.expires_at && (
              <span style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                (token expires {new Date(status.expires_at).toLocaleString()})
              </span>
            )}
            <button
              className="btn"
              onClick={handleDisconnect}
              disabled={disconnecting}
              style={{ fontSize: "0.8rem", padding: "0.25rem 0.75rem" }}
            >
              {disconnecting ? "Disconnecting..." : "Disconnect"}
            </button>
          </>
        ) : status?.connected && status.method === "pat" ? (
          <>
            <span style={{ color: "var(--warning, orange)", fontSize: "0.875rem" }}>
              ● Using legacy PAT
            </span>
            <button
              className="btn btn-primary"
              onClick={handleConnect}
              disabled={connecting}
              style={{ fontSize: "0.8rem", padding: "0.25rem 0.75rem" }}
            >
              {connecting ? "Redirecting..." : "Upgrade to OAuth"}
            </button>
          </>
        ) : (
          <>
            <span style={{ color: "var(--text-muted)", fontSize: "0.875rem" }}>
              Not connected
            </span>
            <button
              className="btn btn-primary"
              onClick={handleConnect}
              disabled={connecting}
              style={{ fontSize: "0.8rem", padding: "0.25rem 0.75rem" }}
            >
              {connecting ? "Redirecting..." : "Connect with OAuth"}
            </button>
          </>
        )}
      </div>

      {error && (
        <p style={{ color: "var(--danger)", fontSize: "0.8rem", marginTop: "0.5rem" }}>
          {error}
        </p>
      )}
    </div>
  );
}
