import { expect, test, type Page } from "./fixtures";

for (const refusal of [
  { status: 409, detail: { code: "unresolved_safety_reverts", action_id: 11763 }, message: "Reset blocked: an unresolved safety restore (action 11763) must be settled first." },
  { status: 503, detail: { code: "ml_lifecycle_unavailable" }, message: "Reset unavailable: model lifecycle storage could not be reached. Try again later." },
  { status: 409, detail: { code: "unresolved_safety_reverts", action_id: { unsafe: true } }, message: "Reset blocked: an unresolved safety restore must be settled first." },
  { status: 400, detail: "A readable refusal", message: "A readable refusal" },
  { status: 500, detail: ["not", "a", "message"], message: "Reset failed" },
  { status: 500, detail: null, message: "Reset failed" },
  { status: 500, detail: { unexpected: true }, message: "Reset failed" },
]) {
  test(`reset renders safe refusal: ${JSON.stringify(refusal.detail)}`, async ({ page }) => {
    await mockSettings(page);
    await page.route("**/api/admin/reset", (route) => route.fulfill({
      status: refusal.status, contentType: "application/json",
      body: JSON.stringify({ detail: refusal.detail }),
    }));
    page.on("dialog", (dialog) => dialog.accept());
    await page.goto("/settings");
    const selected = page.getByRole("checkbox", { name: /Device status history/ });
    await selected.check();
    await page.getByRole("button", { name: "Reset Selected Data" }).click();
    await expect(page.getByText(refusal.message, { exact: true })).toBeVisible();
    await expect(selected).toBeChecked();
    await expect(page.getByRole("button", { name: "Reset Selected Data" })).toBeEnabled();
  });
}

const settings = {
  ml_maintenance_enabled: { value: "false", type: "bool", description: "Model maintenance" },
  seasonal_calibration_auto_exit: { value: "true", type: "bool", description: "Legacy automatic exit", read_only: true },
  price_provider: { value: "manual", type: "text", description: "Price provider", options: ["manual", "entsoe", "tibber"] },
  manual_price_eur_per_kwh: { value: "0.12", type: "float", description: "Manual price" },
  entsoe_api_token: { value: "***", type: "secret", description: "ENTSO-E token" },
  entsoe_area: { value: "SE1", type: "text", description: "ENTSO-E area" },
  tank_min_temp: { value: "45", type: "float", description: "Minimum tank temperature" },
  tank_max_temp: { value: "55", type: "float", description: "Maximum tank temperature" },
  learned_schedule_threshold: { value: "0.5", type: "float", description: "Learned schedule threshold", min: 0, max: 1 },
};

async function mockSettings(
  page: Page,
  onSave?: (payload: Record<string, string>) => void,
  saveResponse: { status?: number; body?: unknown } = {},
) {
  let persisted = settings;
  await page.route("**/api/**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "{}" }));
  await page.route("**/api/settings", (route) => {
    if (route.request().method() === "PUT") {
      const payload: Record<string, string> = JSON.parse(route.request().postData() || "{}").settings;
      onSave?.(payload);
      if ((saveResponse.status ?? 200) < 400) {
        for (const [key, value] of Object.entries(payload)) {
          if (key in persisted) {
            const settingKey = key as keyof typeof settings;
            persisted = { ...persisted, [settingKey]: { ...persisted[settingKey], value } };
          }
        }
      }
      return route.fulfill({
        status: saveResponse.status ?? 200,
        contentType: "application/json",
        body: JSON.stringify(saveResponse.body ?? persisted),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(persisted),
    });
  });
  await page.route("**/api/currency", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ code: "EUR" }) }));
  await page.route("**/api/logs*", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/api/comfort-schedule", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ weekday: [], weekend: [] }) }));
  await page.route("**/api/comfort-schedule/learned", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ weekday: {}, weekend: {} }) }));
}

test("model maintenance defaults off and saves only explicit enablement", async ({ page }, testInfo) => {
  let saved: Record<string, string> | undefined;
  await mockSettings(page, (payload) => { saved = payload; }, {
    body: { ...settings, ml_maintenance_enabled: { ...settings.ml_maintenance_enabled, value: "true" } },
  });
  await page.goto("/settings");
  const toggle = page.getByRole("checkbox", { name: "Model maintenance", exact: true });
  await expect(toggle).not.toBeChecked();
  await toggle.check();
  await page.getByRole("button", { name: /Save 1 change/ }).click();
  await expect.poll(() => saved).toEqual({ ml_maintenance_enabled: "true" });
  await expect(toggle).toBeChecked();
  await page.reload();
  await expect(toggle).toBeChecked();
  await toggle.scrollIntoViewIfNeeded();
  const box = await toggle.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(await page.evaluate(() => window.innerWidth));
  await page.screenshot({ path: testInfo.outputPath("ml-maintenance-settings.png") });
});

test("maintenance defaults off and legacy auto-exit is read-only and ineffective", async ({ page }, testInfo) => {
  let saved: Record<string, string> | undefined;
  await mockSettings(page, (payload) => { saved = payload; });
  await page.goto("/settings");
  await expect(page.getByRole("checkbox", { name: "Model maintenance", exact: true })).not.toBeChecked();
  await page.getByRole("button", { name: "Show advanced" }).click();
  const autoExit = page.getByRole("checkbox", { name: "Automatic observation exit", exact: true });
  await expect(autoExit).toBeDisabled();
  await expect(autoExit).not.toBeChecked();
  await expect(page.getByText("Always false. Observation persists until explicit human exit; legacy stored values are ineffective.")).toBeVisible();
  await page.getByRole("checkbox", { name: "Model maintenance", exact: true }).check();
  await page.getByRole("button", { name: /Save 1 change/ }).click();
  await expect.poll(() => saved).toEqual({ ml_maintenance_enabled: "true" });
  await expect(autoExit).toBeDisabled();
  await expect(autoExit).not.toBeChecked();
  await autoExit.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("obs6-auto-exit-disabled.png") });
});

test("settings exposes one skip target and validates without sending invalid drafts", async ({ page }) => {
  let saves = 0;
  await mockSettings(page, () => { saves += 1; });
  await page.goto("/settings");

  await expect(page.locator("main#main-content")).toHaveCount(1);
  await expect(page.locator(".skip-link")).toHaveAttribute("href", "#main-content");
  await page.locator("#setting-tank_min_temp").fill("70");
  const save = page.getByRole("button", { name: /Save 1 change/ });
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.locator(".override-banner[role=alert]")).toContainText("Fix 1 invalid field");
  await expect(page.locator("#setting-tank_min_temp")).toBeFocused();
  expect(saves).toBe(0);
});

test("settings tabs follow valid URL history and ignore invalid tab values", async ({ page }) => {
  await mockSettings(page);
  await page.goto("/settings?tab=integrations");
  await expect(page.getByRole("tab", { name: "Integrations" })).toHaveAttribute("aria-selected", "true");

  await page.evaluate(() => {
    window.history.pushState({}, "", "/settings?tab=display");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.getByRole("tab", { name: "Display" })).toHaveAttribute("aria-selected", "true");

  await page.goto("/settings?tab=not-a-tab");
  await expect(page.getByRole("tab", { name: "Optimizer" })).toHaveAttribute("aria-selected", "true");
});

test("settings focus styles expose the main target and a two-pixel focus offset", async ({ page }) => {
  await mockSettings(page);
  await page.goto("/settings");

  await page.locator(".skip-link").focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("main#main-content")).toBeFocused();

  const focusStyle = await page.locator("#setting-tank_min_temp").evaluate((input) => {
    input.focus();
    const style = getComputedStyle(input);
    return { outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth, outlineOffset: style.outlineOffset };
  });
  expect(focusStyle.outlineStyle).toBe("solid");
  expect(parseFloat(focusStyle.outlineWidth)).toBeGreaterThanOrEqual(2);
  expect(parseFloat(focusStyle.outlineOffset)).toBeGreaterThanOrEqual(2);
});

test("settings warns before unload only while an applicable draft is dirty", async ({ page }) => {
  await mockSettings(page);
  await page.goto("/settings?tab=data");

  const cleanUnload = await page.evaluate(() => window.dispatchEvent(new Event("beforeunload", { cancelable: true })));
  expect(cleanUnload).toBe(true);

  await page.locator("#setting-manual_price_eur_per_kwh").fill("0.20");
  const dirtyUnload = await page.evaluate(() => {
    const event = new Event("beforeunload", { cancelable: true });
    return { dispatchResult: window.dispatchEvent(event), defaultPrevented: event.defaultPrevented };
  });
  expect(dirtyUnload.defaultPrevented).toBe(true);
});

test("settings renders unique ids for one control per setting", async ({ page }) => {
  await mockSettings(page);
  await page.goto("/settings");
  const duplicateIds = await page.locator("[id]").evaluateAll((elements) => {
    const counts = new Map<string, number>();
    for (const element of elements) counts.set(element.id, (counts.get(element.id) ?? 0) + 1);
    return [...counts.entries()].filter(([, count]) => count > 1).map(([id]) => id);
  });
  expect(duplicateIds).toEqual([]);
});

test("settings exposes field errors and counts them on the owning tab", async ({ page }) => {
  await mockSettings(page);
  await page.goto("/settings");

  await page.locator("#setting-tank_min_temp").fill("70");

  await expect(page.locator("#setting-tank_min_temp")).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#setting-error-tank_min_temp")).toBeVisible();
  await expect(page.locator("#setting-tank_min_temp")).toHaveAttribute(
    "aria-describedby",
    "setting-error-tank_min_temp",
  );
  await expect(page.getByRole("tab", { name: /Optimizer \(1 errors\)/ })).toBeVisible();
});

test("settings sends only applicable dirty values", async ({ page }) => {
  let saved: Record<string, string> | undefined;
  await mockSettings(page, (payload) => { saved = payload; });
  await page.goto("/settings?tab=data");
  await page.locator("#setting-manual_price_eur_per_kwh").fill("0.20");
  await page.getByRole("button", { name: /Save 1 change/ }).click();
  await expect.poll(() => saved).toEqual({ manual_price_eur_per_kwh: "0.20" });
});

test("R04 saves a finite zero price and retains it after reload", async ({ page }, testInfo) => {
  let saved: Record<string, string> | undefined;
  await mockSettings(page, (payload) => { saved = payload; });
  await page.goto("/settings?tab=data");
  const input = page.locator("#setting-manual_price_eur_per_kwh");
  await input.fill("0");
  await page.getByRole("button", { name: /Save 1 change/ }).click();
  await expect.poll(() => saved).toEqual({ manual_price_eur_per_kwh: "0" });
  await expect(page.getByRole("status").filter({ hasText: "Saved 1 setting(s)" })).toBeVisible();
  await page.reload();
  await expect(input).toHaveValue("0");
  await expect(page.getByRole("button", { name: "Save  change" })).toBeDisabled();
  await input.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("r04-finite-zero-saved.png") });
});

test("settings filters values that become inapplicable and masked secrets", async ({ page }) => {
  let saved: Record<string, string> | undefined;
  await mockSettings(page, (payload) => { saved = payload; });
  await page.goto("/settings?tab=data");

  await page.locator("#setting-manual_price_eur_per_kwh").fill("0.20");
  await page.locator("#setting-price_provider").selectOption("entsoe");
  await page.locator("#setting-entsoe_api_token").fill("new-token");
  await page.locator("#setting-entsoe_area").fill("SE2");

  await expect(page.getByRole("button", { name: /Save 3 changes/ })).toBeEnabled();
  await page.getByRole("button", { name: /Save 3 changes/ }).click();
  await expect.poll(() => saved).toEqual({
    price_provider: "entsoe",
    entsoe_api_token: "new-token",
    entsoe_area: "SE2",
  });
});

for (const failure of [
  { name: "API save failure", status: 500, detail: "Backend unavailable", message: "Backend unavailable" },
  {
    name: "R04 structured numeric refusal", status: 400,
    detail: { invalid_values: { manual_price_eur_per_kwh: "Value must be finite" } },
    message: "manual_price_eur_per_kwh: Value must be finite"
  },
  {
    name: "R04 multi-field numeric refusal", status: 400,
    detail: { invalid_values: { manual_price_eur_per_kwh: "Value must be finite", tank_min_temp: "Value must be at least 20" } },
    message: "manual_price_eur_per_kwh: Value must be finite; tank_min_temp: Value must be at least 20"
  },
  {
    name: "R04 empty numeric refusal", status: 400,
    detail: { invalid_values: {} }, message: "Save failed"
  },
  {
    name: "R04 malformed numeric refusal", status: 400,
    detail: { invalid_values: ["Value must be finite"] }, message: "Save failed"
  },
  {
    name: "R04 unknown structured refusal", status: 400,
    detail: { code: "invalid_numeric_setting" }, message: "Save failed"
  },
  {
    name: "R04 null structured refusal", status: 400,
    detail: null, message: "Save failed"
  },
]) {
  test(`settings retains the draft after ${failure.name}`, async ({ page }) => {
    await mockSettings(page, undefined, { status: failure.status, body: { detail: failure.detail } });
    await page.goto("/settings?tab=data");
    const input = page.locator("#setting-manual_price_eur_per_kwh");
    await input.fill("0.20");

    await page.getByRole("button", { name: /Save 1 change/ }).click();

    await expect(page.locator(".override-banner[role=alert]")).toContainText(failure.message);
    await expect(input).toHaveValue("0.20");
    await expect(page.getByRole("button", { name: /Save 1 change/ })).toBeEnabled();
  });
}

test("advanced validation focuses the first applicable invalid field without a PUT", async ({ page }) => {
  let saves = 0;
  await mockSettings(page, () => { saves += 1; });
  await page.goto("/settings");

  await page.getByRole("button", { name: "Show advanced" }).click();
  const input = page.locator("#setting-learned_schedule_threshold");
  await input.fill("2");
  await page.getByRole("button", { name: /Save 1 change/ }).click();

  await expect(page.locator(".override-banner[role=alert]")).toContainText("Fix 1 invalid field");
  await expect(input).toBeFocused();
  expect(saves).toBe(0);
});

test("settings focuses the first invalid field in declared order", async ({ page }) => {
  let saves = 0;
  await mockSettings(page, () => { saves += 1; });
  await page.goto("/settings");

  await page.locator("#setting-tank_min_temp").fill("70");
  await page.locator("#setting-tank_max_temp").fill("20");
  await page.getByRole("button", { name: /Save 2 changes/ }).click();

  await expect(page.locator(".override-banner[role=alert]")).toContainText("Fix 2 invalid field(s)");
  await expect(page.locator("#setting-tank_min_temp")).toBeFocused();
  expect(saves).toBe(0);
});

test("comfort schedule has a single roving cell and 44px touch targets", async ({ page }) => {
  await mockSettings(page);
  await page.goto("/settings");
  const cells = page.locator("#comfort-schedule").getByRole("checkbox");
  await expect(cells).toHaveCount(48);
  await expect(page.locator('[role="checkbox"][tabindex="0"]')).toHaveCount(1);
  const size = await cells.first().evaluate((cell) => {
    const rect = cell.getBoundingClientRect();
    return { width: rect.width, height: rect.height };
  });
  expect(size.width).toBeGreaterThanOrEqual(44);
  expect(size.height).toBeGreaterThanOrEqual(44);
  await cells.first().focus();
  await page.keyboard.press("ArrowRight");
  await expect(cells.nth(1)).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(cells.nth(25)).toBeFocused();
});

test("comfort schedule keyboard interaction changes only selection state", async ({ page }) => {
  await mockSettings(page);
  await page.goto("/settings");
  const cells = page.locator("#comfort-schedule").getByRole("checkbox");

  await cells.first().focus();
  await page.keyboard.press("ArrowRight");
  await expect(cells.nth(1)).toBeFocused();
  await expect(cells.nth(1)).toHaveAttribute("aria-checked", "false");

  await page.keyboard.press("Space");
  await expect(cells.nth(1)).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("Enter");
  await expect(cells.nth(1)).toHaveAttribute("aria-checked", "false");

  await page.keyboard.press("Home");
  await expect(cells.first()).toBeFocused();
  await page.keyboard.press("End");
  await expect(cells.nth(23)).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(cells.nth(47)).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await expect(cells.nth(23)).toBeFocused();
});

test("comfort schedule syncs the roving cell when selected with the mouse", async ({ page }) => {
  await mockSettings(page);
  await page.goto("/settings");
  const cells = page.locator("#comfort-schedule").getByRole("checkbox");

  await cells.nth(30).click();

  await expect(cells.nth(30)).toBeFocused();
  await expect(page.locator('[role="checkbox"][tabindex="0"]')).toHaveCount(1);
  await expect(cells.nth(30)).toHaveAttribute("tabindex", "0");
});

test("comfort schedule clamps learned hours before saving", async ({ page }) => {
  let savedSchedule: unknown;
  await mockSettings(page);
  await page.route("**/api/comfort-schedule/apply-learned", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ weekday: [-1, 0, 23, 24], weekend: [25] }),
  }));
  await page.route("**/api/comfort-schedule", (route) => {
    if (route.request().method() === "PUT") {
      savedSchedule = JSON.parse(route.request().postData() || "{}");
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ weekday: [], weekend: [] }),
    });
  });
  await page.goto("/settings");

  const applyLearned = page.getByRole("button", { name: "Apply Learned Schedule" });
  await applyLearned.click();
  await expect(applyLearned).toBeFocused();
  await page.getByRole("button", { name: "Save Schedule" }).click();
  await expect.poll(() => savedSchedule).toEqual({ weekday: [0, 23], weekend: [] });
});