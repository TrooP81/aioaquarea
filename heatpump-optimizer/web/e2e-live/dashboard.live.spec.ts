import { test, expect } from "@playwright/test";
import { getJson } from "./_live";

/**
 * Drives the real dashboard at the live web origin (no mocking) and ties the
 * rendered UI back to the actual backend state.
 */
test.describe("Live dashboard", () => {
  test("renders header and a status badge that matches backend state", async ({ page, request }) => {
    const dash = await getJson<{ current_status: unknown }>(request, "/api/dashboard");

    await page.goto("/");
    await expect(page.locator("h1")).toContainText("Heat Pump Optimizer");

    const badge = page.locator(".status-badge");
    await expect(badge).toBeVisible({ timeout: 15000 });

    // The badge must reflect whether the backend actually has a device status.
    if (dash.current_status) {
      await expect(page.locator(".status-badge.online")).toContainText("Connected");
    } else {
      await expect(page.locator(".status-badge.offline")).toContainText("Disconnected");
    }
  });

  test("does not show an API error banner against the live backend", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("h1")).toContainText("Heat Pump Optimizer");
    // Give the dashboard a moment to finish its initial fetches.
    await expect(page.locator(".status-badge")).toBeVisible({ timeout: 15000 });
    await expect(page.locator(".override-banner", { hasText: "API Error" })).toHaveCount(0);
  });

  test("learning-mode banner matches the live learning-mode flag", async ({ page, request }) => {
    const lm = await getJson<{ enabled: boolean }>(request, "/api/learning-mode");

    await page.goto("/");
    await expect(page.locator(".status-badge")).toBeVisible({ timeout: 15000 });

    const banner = page.locator(".override-banner", { hasText: "Learning mode active" });
    if (lm.enabled) {
      await expect(banner).toBeVisible();
    } else {
      await expect(banner).toHaveCount(0);
    }
  });

  test("charts section renders a thermal predictions chart", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Thermal Predictions" })).toBeVisible({ timeout: 15000 });
    // Recharts renders SVG; at least one chart must paint with live data.
    await expect(page.locator("svg").first()).toBeVisible({ timeout: 15000 });
  });
});
