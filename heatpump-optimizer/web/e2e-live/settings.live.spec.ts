import { test, expect } from "@playwright/test";

/**
 * Loads the real settings page against the live stack and verifies it renders
 * the settings groups and admin cards backed by the actual settings API.
 */
test.describe("Live settings page", () => {
  test("renders settings groups from the live settings API", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.locator("h1")).toContainText("Settings");

    // Loading state must resolve into real grouped settings.
    await expect(page.getByRole("heading", { name: "Optimizer Layer" })).toBeVisible({ timeout: 15000 });
    await page.getByRole("tab", { name: "Data Sources", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Price Provider" })).toBeVisible();
    await page.getByRole("tab", { name: "Integrations", exact: true }).click();
    await expect(page.getByRole("heading", { name: "SmartThings Integration" })).toBeVisible();

    // The save control is present (settings form fully rendered).
    await expect(page.getByRole("button", { name: "Save changes", exact: true })).toBeVisible();
  });

  test("shows the reset-data danger zone", async ({ page }) => {
    await page.goto("/settings");
    await page.getByRole("tab", { name: "System", exact: true }).click();
    await expect(page.getByText("Danger Zone — Reset Data")).toBeVisible({ timeout: 15000 });
  });

  test("navigates back to the dashboard", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.locator("h1")).toContainText("Settings");
    await page.getByRole("link", { name: "← Dashboard" }).click();
    await expect(page.locator("h1")).toContainText("Heat Pump Optimizer");
  });
});
