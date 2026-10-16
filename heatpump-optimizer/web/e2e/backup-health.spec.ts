import { test, expect } from "./fixtures";

const backup = {
  ready: true, status: "healthy", reason_codes: [],
  last_success: "2026-10-16T12:00:00Z",
  last_attempt: { started_at: "2026-10-16T12:00:00Z", finished_at: "2026-10-16T12:01:00Z", status: "succeeded" },
  last_verification: { started_at: "2026-10-16T12:02:00Z", finished_at: "2026-10-16T12:03:00Z", status: "succeeded" },
  last_verified: "2026-10-16T12:03:00Z", success_age_seconds: 3600,
  stale_after_seconds: 2764800, next_due_at: "2026-11-15T12:00:00Z", restore_qualified: true,
};

test("separately labels all three histories and a recent failed attempt", async ({ page }) => {
  await page.route("**/api/operations/alerts", route => route.fulfill({ json: {
    enabled: true, alerts: [], backup: { ...backup, ready: false, status: "degraded",
      reason_codes: ["backup_attempt_failed"],
      last_attempt: { ...backup.last_attempt, status: "failed" } },
  } }));
  await page.goto("/");
  const summary = page.getByLabel("Database backup health");
  await expect(summary).toContainText("Successful backup");
  await expect(summary).toContainText("Latest attempt");
  await expect(summary).toContainText("Verified restore");
  await expect(summary).toContainText("failed");
  await expect(summary).toContainText("Needs attention");
});

for (const scenario of ["missing", "disabled", "stale", "error"] as const) {
  test(`does not infer healthy protection from ${scenario} alerts`, async ({ page }) => {
    await page.route("**/api/operations/alerts", route => route.fulfill({
      status: scenario === "error" ? 503 : 200,
      json: { enabled: scenario !== "disabled", alerts: [],
        ...(scenario === "stale" ? { backup: { ...backup, ready: false, status: "degraded",
          reason_codes: ["backup_success_stale"] } } : {}) },
    }));
    await page.goto("/");
    if (scenario === "error") {
      await expect(page.getByText("Operational alerts could not be loaded.")).toBeVisible();
    } else {
      await expect(page.getByLabel("Database backup health")).not.toContainText("Healthy");
    }
  });
}

test("existing refresh observes changed backup evidence", async ({ page }) => {
  await page.clock.install();
  let failed = false;
  await page.route("**/api/operations/alerts", route => route.fulfill({ json: {
    enabled: true, alerts: [], backup: failed
      ? { ...backup, ready: false, status: "degraded", reason_codes: ["backup_attempt_failed"] }
      : backup,
  } }));
  await page.goto("/");
  await expect(page.getByLabel("Database backup health")).toContainText("Healthy");
  failed = true;
  await page.clock.fastForward(31000);
  await expect(page.getByLabel("Database backup health")).toContainText("Needs attention");
});
