import { defineConfig, devices } from "@playwright/test";

/**
 * Live-stack end-to-end config.
 *
 * Unlike `playwright.config.ts` (which mocks every API call and starts its own
 * `next dev` server), this config drives the **real running stack**: the web
 * container on :4444 proxying `/api/*` to the API container on :8500, backed by
 * the real database, optimizer and ML models. It starts no server of its own —
 * the Docker stack must already be up (`docker compose up -d`).
 *
 * Override targets with E2E_BASE_URL / E2E_API_URL when running elsewhere.
 */
const WEB_BASE = process.env.E2E_BASE_URL ?? "http://localhost:4444";

export default defineConfig({
  testDir: "./e2e-live",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: WEB_BASE,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
