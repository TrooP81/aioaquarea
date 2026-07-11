import { APP_VERSION } from "@/lib/release";

export function AppVersionBadge() {
  return (
    <span
      className="app-version-badge"
      data-testid="app-version"
      title={`Live dashboard build: version ${APP_VERSION}`}
    >
      <span className="app-version-label">Live UI</span>
      <strong>v{APP_VERSION}</strong>
    </span>
  );
}
