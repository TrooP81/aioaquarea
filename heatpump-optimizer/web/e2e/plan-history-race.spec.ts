import { expect, test } from "@playwright/test";

const now = new Date();

const plans = [
  {
    id: 101,
    created_at: now.toISOString(),
    horizon_start: now.toISOString(),
    horizon_end: new Date(now.getTime() + 86_400_000).toISOString(),
    optimizer_version: "rules_v1",
    cost_estimate_eur: 1.2,
    actions_count: 1,
  },
  {
    id: 202,
    created_at: new Date(now.getTime() - 86_400_000).toISOString(),
    horizon_start: now.toISOString(),
    horizon_end: new Date(now.getTime() + 86_400_000).toISOString(),
    optimizer_version: "milp_v1",
    cost_estimate_eur: 1.5,
    actions_count: 1,
  },
];

function action(reason: string) {
  return {
    id: 1,
    scheduled_ts: new Date(now.getTime() + 3_600_000).toISOString(),
    action_type: "force_dhw_on",
    payload: { reason },
    status: "pending",
    executed_at: null,
    result: null,
  };
}

test("plan history ignores a late response from a previously expanded plan", async ({ page }) => {
  await page.route("**/api/dashboard", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        current_status: null,
        current_price: null,
        today_kwh: 0,
        today_cost_eur: 0,
        active_plan: null,
        has_override: false,
      }),
    })
  );
  await page.route(/\/api\/plans\?limit=\d+$/, (route) =>
    route.fulfill({ contentType: "application/json", body: JSON.stringify(plans) })
  );
  await page.route("**/api/plans/101", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 300));
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ...plans[0], actions: [action("first_plan_only")] }),
    });
  });
  await page.route("**/api/plans/202", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ ...plans[1], actions: [action("second_plan_only")] }),
    })
  );

  await page.goto("/");
  await page.getByRole("tab", { name: "Plan" }).click();
  const rows = page.locator(".plan-history-row");
  await expect(rows).toHaveCount(2);

  await rows.nth(0).click();
  await rows.nth(1).click();

  await expect(page.getByText("second plan only")).toBeVisible();
  await page.waitForTimeout(350);
  await expect(page.getByText("first plan only")).not.toBeVisible();
});
