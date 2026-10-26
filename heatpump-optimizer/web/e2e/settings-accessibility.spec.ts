import { expect, test, type Page } from "./fixtures";
import type { Route } from "@playwright/test";

const settings = {
  price_provider: { value: "manual", type: "text", description: "Price provider", options: ["manual", "entsoe", "tibber"] },
  manual_price_eur_per_kwh: { value: "0.12", type: "float", description: "Manual price" },
  entsoe_api_token: { value: "***", type: "secret", description: "ENTSO-E token" },
  entsoe_area: { value: "SE1", type: "text", description: "ENTSO-E area" },
  tank_min_temp: { value: "45", type: "float", description: "Minimum tank temperature" },
  tank_max_temp: { value: "55", type: "float", description: "Maximum tank temperature" },
  learned_schedule_threshold: { value: "0.5", type: "float", description: "Learned schedule threshold", min: 0, max: 1 },
};

const installationId = "00000000-0000-0000-0000-000000000017";
const planningContract = {
  timezone: "UTC",
  comfort_schedule: { weekday: [7], weekend: [8] },
  comfort_temp_min: 18,
  comfort_temp_target: 20.5,
  comfort_temp_max: 25,
  revision_id: 7,
};

function comfortSection(page: Page) {
  return page.locator(".plan-section").filter({ has: page.getByRole("heading", { name: "Comfort Schedule", exact: true }) });
}

function previewBody(revision = 7, timezone = "UTC") {
  return { target_schedule: [], base_revision_id: revision, proposed_contract: { timezone } };
}

async function releasePreview(page: Page, route: Route, body: unknown, status = 200) {
  const received = page.waitForResponse((response) => response.url() === route.request().url());
  await route.fulfill({ status, json: body });
  await (await received).finished();
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

async function mockSettings(
  page: Page,
  onSave?: (payload: Record<string, string>) => void,
  saveResponse: { status?: number; body?: unknown } = {},
) {
  await page.route("**/api/**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "{}" }));
  await page.route("**/api/settings", (route) => {
    if (route.request().method() === "PUT") {
      onSave?.(JSON.parse(route.request().postData() || "{}").settings);
      return route.fulfill({
        status: saveResponse.status ?? 200,
        contentType: "application/json",
        headers: { "X-Contract-Revision": "7" },
        body: JSON.stringify(saveResponse.body ?? settings),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(settings),
      headers: { "X-Installation-ID": installationId, "X-Contract-Revision": "7" },
    });
  });
  await page.route("**/api/currency", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ code: "EUR" }) }));
  await page.route("**/api/logs*", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/api/comfort-schedule", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ weekday: [], weekend: [] }) }));
  await page.route("**/api/comfort-schedule/learned", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ weekday: {}, weekend: {} }) }));
  await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    headers: { ETag: '"7"', "X-Contract-Revision": "7" },
    body: JSON.stringify(planningContract),
  }));
  await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => {
    const payload = JSON.parse(route.request().postData() || "{}");
    const schedule = payload.comfort_schedule ?? planningContract.comfort_schedule;
    const targetSchedule = [
      { comfort_hour: schedule.weekday.includes(7), target: 20.5, local_start: "2026-01-05T07:00", local_end: "2026-01-05T08:00", interval_start_utc: "2026-01-05T07:00Z", interval_end_utc: "2026-01-05T08:00Z" },
      { comfort_hour: schedule.weekday.includes(8), target: 18, local_start: "2026-01-05T08:00", local_end: "2026-01-05T09:00", interval_start_utc: "2026-01-05T08:00Z", interval_end_utc: "2026-01-05T09:00Z" },
    ];
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        target_schedule: targetSchedule,
        base_revision_id: 7,
        proposed_contract: { timezone: "UTC" },
      }),
    });
  });
}

async function scheduleMask(page: Page) {
  return comfortSection(page).getByRole("checkbox").evaluateAll((cells) =>
    cells.map((cell) => cell.getAttribute("aria-checked") === "true"));
}

function expectedMask(schedule: { weekday: number[]; weekend: number[] }) {
  return [schedule.weekday, schedule.weekend].flatMap((hours) =>
    Array.from({ length: 24 }, (_, hour) => hours.includes(hour)));
}

const untrustedRevisionHeaders: Record<string, string>[] = [
  {},
  { "X-Contract-Revision": "invalid" },
  { "X-Contract-Revision": "0" },
  { ETag: 'W/"7"' },
  { ETag: '"7' },
  { "X-Contract-Revision": "7", ETag: '"8"' },
];
for (const invalidHeaders of untrustedRevisionHeaders) {
  test(`R11-Q01 parent rejects untrusted revision ${JSON.stringify(invalidHeaders)} and preserves concurrent draft`, async ({ page }) => {
    await mockSettings(page);
    const values = { ...settings, comfort_temp_min: { value: "18", type: "float", description: "Comfort minimum" } };
    let recovering = false;
    let recovered = false;
    let recovery: Route | undefined;
    const writes: Array<{ revision: string | undefined; settings: Record<string, string> }> = [];
    await page.route("**/api/settings", (route) => {
      if (route.request().method() === "PUT") {
        writes.push({
          revision: route.request().headers()["if-match"],
          settings: JSON.parse(route.request().postData()!).settings,
        });
        values.comfort_temp_min.value = "20";
        return route.fulfill({ json: {}, headers: { "X-Contract-Revision": "9" } });
      }
      if (recovering && !recovered) {
        recovery = route;
        return;
      }
      return route.fulfill({
        json: values,
        headers: { "X-Installation-ID": installationId, ...(recovered ? { "X-Contract-Revision": "9" } : invalidHeaders) },
      });
    });
    await page.goto("/settings");
    const input = page.locator("#setting-comfort_temp_min");
    await input.fill("19");
    const save = page.getByRole("button", { name: "Save 1 change", exact: true });
    await expect(save).toBeDisabled();
    await save.dispatchEvent("click");
    expect(writes).toEqual([]);
    recovering = true;
    await page.locator(".settings-action-bar").getByRole("button", { name: "Reload planning settings", exact: true }).click();
    await expect.poll(() => !!recovery).toBe(true);
    await input.fill("20"); // Editing during the held refresh must survive too.
    await save.dispatchEvent("click");
    expect(writes).toEqual([]);
    recovered = true;
    values.comfort_temp_min.value = "18.5"; // A concurrent writer's value, not our draft.
    await recovery!.fulfill({ json: values, headers: { "X-Contract-Revision": "8" } });
    await expect(input).toHaveValue("20");
    const comparison = page.getByRole("alert").filter({ hasText: "Current saved planning settings differ" });
    await expect(comparison).toContainText("saved “18.5”; your draft “20”");
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect(save).toBeDisabled();
    await save.dispatchEvent("click");
    expect(writes).toEqual([]);
    await comparison.getByRole("button", { name: "Save my draft over this revision" }).click();
    await expect.poll(() => writes.length).toBe(1);
    expect(writes).toEqual([{ revision: "8", settings: { comfort_temp_min: "20" } }]);
    await expect(page.locator(".override-banner[role=status]")).toContainText("Saved 1 setting");
  });
}

test("R11-Q01 parent stale revision requires explicit comparison before versioned retry", async ({ page }) => {
  await mockSettings(page);
  let revision = "7";
  const values = {
    ...settings,
    use_comfort_model: { value: "true", type: "bool", description: "Use comfort model" },
    comfort_temp_target: { value: "20.5", type: "float", description: "Comfort target" },
  };
  const writes: string[] = [];
  let holdingConflict = false;
  let conflictRefresh: Route | undefined;
  await page.route("**/api/settings", (route) => {
    if (route.request().method() === "PUT") {
      writes.push(route.request().headers()["if-match"]);
      expect(JSON.parse(route.request().postData()!).settings).toEqual({ comfort_temp_target: writes.length === 1 ? "23" : "24" });
      if (writes.length === 1) {
        revision = "8";
        values.comfort_temp_target.value = "22";
        holdingConflict = true;
        return route.fulfill({ status: 409, json: { detail: "changed elsewhere" } });
      }
      holdingConflict = false;
      values.comfort_temp_target.value = "24";
      revision = "9";
      return route.fulfill({ json: {}, headers: { ETag: '"9"' } });
    }
    if (holdingConflict) { conflictRefresh = route; return; }
    return route.fulfill({ json: values, headers: { "X-Installation-ID": installationId, ETag: `"${revision}"` } });
  });
  await page.goto("/settings");
  await page.getByRole("button", { name: "Show advanced" }).click();
  await page.locator("#setting-comfort_temp_target").fill("23");
  const save = page.getByRole("button", { name: "Save 1 change", exact: true });
  await save.click();
  await expect.poll(() => !!conflictRefresh).toBe(true);
  await page.locator("#setting-comfort_temp_target").fill("24");
  await conflictRefresh!.fulfill({ json: values, headers: { ETag: '"8"' } });
  const comparison = page.getByRole("alert").filter({ hasText: "Current saved planning settings differ" });
  await expect(comparison).toContainText("saved “22”; your draft “24”");
  await expect(save).toBeDisabled();
  await save.dispatchEvent("click");
  expect(writes).toEqual(["7"]);
  await comparison.getByRole("button", { name: "Save my draft over this revision" }).click();
  await expect.poll(() => writes).toEqual(["7", "8"]);
  await expect(page.locator(".override-banner[role=status]")).toContainText("Saved 1 setting");
});

for (const failure of ["missing-revision", "http-error", "mismatched-headers"] as const) {
  test(`R11-Q01 parent ${failure} on conflict refresh cannot send unversioned retry`, async ({ page }) => {
    await mockSettings(page);
    let conflict = false;
    let writes = 0;
    await page.route("**/api/settings", (route) => {
      if (route.request().method() === "PUT") {
        expect(route.request().headers()["if-match"]).toBe("7");
        writes += 1;
        conflict = true;
        return route.fulfill({ status: 409, json: {} });
      }
      return route.fulfill({
        status: conflict && failure === "http-error" ? 503 : 200,
        json: settings,
        headers: {
          "X-Installation-ID": installationId,
          ...(!conflict ? { "X-Contract-Revision": "7" }
            : failure === "mismatched-headers" ? { "X-Contract-Revision": "8", ETag: '"9"' } : {}),
        },
      });
    });
    await page.goto("/settings?tab=data");
    const input = page.locator("#setting-manual_price_eur_per_kwh");
    await input.fill("0.2");
    const save = page.getByRole("button", { name: "Save 1 change", exact: true });
    await save.click();
    await expect(save).toBeDisabled();
    await expect(page.locator(".settings-action-bar")).toContainText("Settings revision is unavailable");
    await expect(input).toHaveValue("0.2");
    await save.dispatchEvent("click");
    expect(writes).toBe(1);
    await expect(page.getByRole("button", { name: "Save my draft over this revision" })).toHaveCount(0);
  });
}

test("R11-Q01 failed recovery preserves edits until explicit acceptance of the saved revision", async ({ page }) => {
  await mockSettings(page);
  let phase: "initial" | "failed" | "recovered" = "initial";
  let writes = 0;
  await page.route("**/api/settings", (route) => {
    if (route.request().method() === "PUT") { writes += 1; return route.fulfill({ json: {} }); }
    return route.fulfill({
      status: phase === "failed" ? 503 : 200,
      json: settings,
      headers: { "X-Installation-ID": installationId, ...(phase === "recovered" ? { "X-Contract-Revision": "8" } : {}) },
    });
  });
  await page.goto("/settings?tab=data");
  const input = page.locator("#setting-manual_price_eur_per_kwh");
  await input.fill("0.2");
  const reload = page.locator(".settings-action-bar").getByRole("button", { name: "Reload planning settings", exact: true });
  phase = "failed";
  await reload.click();
  await expect(page.locator(".override-banner[role=alert]")).toContainText("Failed to load settings");
  await expect(input).toHaveValue("0.2");
  phase = "recovered";
  await reload.click();
  await expect(input).toHaveValue("0.2");
  await expect(page.getByRole("alert").filter({ hasText: "Current saved planning settings differ" }))
    .toContainText("manual_price_eur_per_kwh: saved “0.12”; your draft “0.2”");
  await page.getByRole("button", { name: "Load saved settings", exact: true }).click();
  await expect(input).toHaveValue("0.12");
  await input.fill("0.3");
  await page.getByRole("button", { name: "Save 1 change", exact: true }).click();
  await expect.poll(() => writes).toBe(1);
});

for (const refresh of ["missing-revision", "failed"] as const) {
  test(`R11-Q01 accepted save with ${refresh} reload cannot reuse the old revision`, async ({ page }) => {
    await mockSettings(page);
    let saved = false;
    let writes = 0;
    await page.route("**/api/settings", (route) => {
      if (route.request().method() === "PUT") {
        expect(route.request().headers()["if-match"]).toBe("7");
        saved = true;
        writes += 1;
        return route.fulfill({ json: {} });
      }
      return route.fulfill({
        json: settings,
        status: saved && refresh === "failed" ? 503 : 200,
        headers: { "X-Installation-ID": installationId, ...(!saved ? { "X-Contract-Revision": "7" } : {}) },
      });
    });
    await page.goto("/settings?tab=data");
    const input = page.locator("#setting-manual_price_eur_per_kwh");
    await input.fill("0.2");
    await page.getByRole("button", { name: "Save 1 change", exact: true }).click();
    await expect(page.locator(".settings-action-bar")).toContainText("Settings revision is unavailable");
    await input.fill("0.3");
    const save = page.getByRole("button", { name: "Save 1 change", exact: true });
    await expect(save).toBeDisabled();
    await save.dispatchEvent("click");
    expect(writes).toBe(1);
  });
}

test("R11-Q01 recovery comparison hides secret values and preserves the versioned secret draft", async ({ page }) => {
  await mockSettings(page);
  const values = {
    ...settings,
    price_provider: { ...settings.price_provider, value: "tibber" },
    tibber_api_token: { value: "***", type: "secret", description: "Tibber token" },
  };
  let recovered = false;
  let submitted: Record<string, string> | undefined;
  await page.route("**/api/settings", (route) => {
    if (route.request().method() === "PUT") {
      expect(route.request().headers()["if-match"]).toBe("8");
      submitted = JSON.parse(route.request().postData()!).settings;
      return route.fulfill({ json: {}, headers: { "X-Contract-Revision": "8" } });
    }
    return route.fulfill({
      json: values,
      headers: { "X-Installation-ID": installationId, ...(recovered ? { "X-Contract-Revision": "8" } : {}) },
    });
  });
  await page.goto("/settings?tab=data");
  await page.locator("#setting-tibber_api_token").fill("fictional-test-token-not-credential");
  recovered = true;
  await page.locator(".settings-action-bar").getByRole("button", { name: "Reload planning settings", exact: true }).click();
  const comparison = page.getByRole("alert").filter({ hasText: "Current saved planning settings differ" });
  await expect(comparison).toContainText("tibber_api_token: saved “(hidden)”; your draft “(hidden)”");
  await expect(comparison).not.toContainText("fictional-test-token-not-credential");
  await comparison.getByRole("button", { name: "Save my draft over this revision" }).click();
  await expect.poll(() => submitted).toEqual({ tibber_api_token: "fictional-test-token-not-credential" });
});

for (const timing of ["learned-response-last", "parent-response-last", "unknown"] as const) {
  test(`R10-Q01 ${timing} across accepted unrelated parent save keeps committed learned result`, async ({ page }) => {
    await mockSettings(page);
    const learned = { weekday: [10, 12], weekend: [11] };
    const values = { ...settings, quiet_mode_start: { value: "22", type: "int", description: "Quiet start" } };
    let current = 7;
    let parentAccepted = false;
    let learnedPost: Route | undefined;
    let parentPut: Route | undefined;
    const posts: string[] = [];
    const puts: Array<{ revision: string; schedule: unknown }> = [];
    const contractReads: number[] = [];
    await page.route("**/api/settings", (route) => {
      if (route.request().method() === "PUT") {
        expect(route.request().headers()["if-match"]).toBe("7");
        expect(JSON.parse(route.request().postData()!).settings).toEqual({ quiet_mode_start: "21" });
        parentPut = route;
        if (timing === "parent-response-last") return;
        parentAccepted = true;
        values.quiet_mode_start.value = "21";
        return route.fulfill({ json: {}, headers: { "X-Contract-Revision": String(current) } });
      }
      return route.fulfill({ json: values, headers: { "X-Installation-ID": installationId, "X-Contract-Revision": String(current) } });
    });
    await page.route("**/api/comfort-schedule/apply-learned", (route) => {
      posts.push(route.request().headers()["if-match"]);
      current = 8; // Commit is deliberately earlier than response delivery.
      learnedPost = route;
      if (timing === "parent-response-last") return route.fulfill({ json: learned });
    });
    await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => {
      expect(route.request().headers()["if-match"]).toBe("8");
      expect(JSON.parse(route.request().postData()!).comfort_schedule).toEqual(learned);
      return route.fulfill({ json: previewBody(8) });
    });
    await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
      const contract = { ...planningContract, revision_id: current, comfort_schedule: current >= 8 ? learned : planningContract.comfort_schedule };
      if (route.request().method() === "PUT") {
        puts.push({ revision: route.request().headers()["if-match"], schedule: JSON.parse(route.request().postData()!).comfort_schedule });
        current = 9;
        return route.fulfill({ json: { ...contract, revision_id: current }, headers: { "X-Contract-Revision": String(current) } });
      }
      contractReads.push(current);
      return route.fulfill({ json: contract, headers: { "X-Contract-Revision": String(current) } });
    });
    await page.goto("/settings");
    const section = comfortSection(page);
    const apply = section.getByRole("button", { name: "Apply Learned Schedule" });
    const save = section.getByRole("button", { name: "Save Schedule", exact: true });
    await expect(save).toBeEnabled();
    await section.getByRole("checkbox").nth(6).click();
    const originalDraft = await scheduleMask(page);
    await page.locator("#setting-quiet_mode_start").fill("21");
    if (timing === "parent-response-last") {
      await page.getByRole("button", { name: "Save 1 change", exact: true }).click();
      await expect.poll(() => !!parentPut).toBe(true);
    }
    await apply.click();
    await expect.poll(() => !!learnedPost).toBe(true);
    if (timing === "parent-response-last") {
      await expect(section).toContainText("Applied learned schedule");
      parentAccepted = true;
      values.quiet_mode_start.value = "21";
      await parentPut!.fulfill({ json: {}, headers: { "X-Contract-Revision": "8" } });
    } else {
      const readsBeforeParent = contractReads.length;
      await page.getByRole("button", { name: "Save 1 change", exact: true }).click();
      await expect.poll(() => parentAccepted).toBe(true);
      await expect(page.locator(".override-banner")).toContainText("Saved 1 setting");
      await expect(section).toContainText("Planning target and timezone metadata unavailable");
      await expect(apply).toBeDisabled();
      // Neither another mutation nor editing can race the pending committed response.
      await apply.dispatchEvent("click");
      await section.getByRole("checkbox").nth(5).dispatchEvent("keydown", { key: " " });
      expect(await scheduleMask(page)).toEqual(originalDraft);
      expect(contractReads).toHaveLength(readsBeforeParent);
      if (timing === "unknown") await learnedPost!.abort("failed");
      else await learnedPost!.fulfill({ json: learned });
    }
    if (timing === "unknown") {
      await expect(section).toContainText("Learned update outcome is unknown");
      await expect(section).toContainText("Saved schedule: weekday 10, 12; weekend 11");
      expect(await scheduleMask(page)).toEqual(originalDraft);
      await expect(save).toBeDisabled();
      await expect(apply).toBeDisabled();
      await apply.dispatchEvent("click");
      await save.dispatchEvent("click");
      expect(puts).toEqual([]);
      expect(posts).toEqual(["7"]);
      await section.getByRole("button", { name: "Load saved schedule", exact: true }).click();
    } else {
      await expect(section).toContainText("Applied learned schedule");
    }
    await expect(save).toBeEnabled();
    expect(await scheduleMask(page)).toEqual(expectedMask(learned));
    await section.getByRole("button", { name: "Preview next 24 hours" }).click();
    await expect(section).toContainText("Preview from revision 8 in UTC");
    const nextSave = page.waitForRequest((request) => request.method() === "PUT" && request.url().endsWith("/planning-settings"));
    await save.click();
    expect((await nextSave).headers()["if-match"]).toBe("8");
    await expect(section).toContainText("Schedule saved");
    expect(puts).toEqual([{ revision: "8", schedule: learned }]);
    expect(posts).toEqual(["7"]);
    expect(contractReads).toContain(8);
    expect(await scheduleMask(page)).toEqual(expectedMask(learned));
  });
}

const invalidLearnedResponses = [
  { name: "empty object", body: "{}" },
  { name: "invalid JSON", body: "not json" },
  { name: "null", body: "null" },
  { name: "array", body: "[]" },
  { name: "missing weekday", body: '{"weekend":[11]}' },
  { name: "missing weekend", body: '{"weekday":[10,12]}' },
  { name: "wrong mask type", body: '{"weekday":"10,12","weekend":[11]}' },
  { name: "wrong hour type", body: '{"weekday":[10,null],"weekend":[11]}' },
  { name: "invalid revision", body: '{"weekday":[10,12],"weekend":[11],"revision_id":"bad"}' },
  { name: "mismatched revision", body: '{"weekday":[10,12],"weekend":[11],"revision_id":9}' },
];

const invalidRecoveryContracts = [
  { name: "missing schedule", patch: { comfort_schedule: undefined } },
  { name: "null schedule", patch: { comfort_schedule: null } },
  { name: "missing weekday", patch: { comfort_schedule: { weekend: [11] } } },
  { name: "missing weekend", patch: { comfort_schedule: { weekday: [10, 12] } } },
  { name: "wrong mask type", patch: { comfort_schedule: { weekday: [10, 12], weekend: "11" } } },
  { name: "invalid hour", patch: { comfort_schedule: { weekday: [24], weekend: [11] } } },
  { name: "missing revision", patch: { revision_id: undefined } },
  { name: "mismatched revision", patch: { revision_id: 9 } },
];

for (const scenario of [
  ...invalidLearnedResponses.map((response) => ({ name: `POST ${response.name}`, response, recovery: null })),
  ...invalidRecoveryContracts.map((recovery) => ({ name: `GET ${recovery.name}`, response: invalidLearnedResponses[0], recovery })),
]) {
  test(`R10-Q01 correction ${scenario.name} retains draft and blocks writes across parent acceptance`, async ({ page }) => {
    await mockSettings(page);
    const learned = { weekday: [10, 12], weekend: [11] };
    const draft = { weekday: [6, 7], weekend: [8] };
    const values = { ...settings, quiet_mode_start: { value: "22", type: "int", description: "Quiet start" } };
    let committed = false;
    let post: Route | undefined;
    let refresh: Route | undefined;
    let recovered = false;
    let posts = 0;
    let puts = 0;
    const previewRevisions: string[] = [];
    await page.route("**/api/settings", (route) => {
      if (route.request().method() === "PUT") {
        expect(JSON.parse(route.request().postData()!).settings).toEqual({ quiet_mode_start: "21" });
        values.quiet_mode_start.value = "21";
        return route.fulfill({ json: {}, headers: { "X-Contract-Revision": "8" } });
      }
      return route.fulfill({ json: values, headers: { "X-Installation-ID": installationId, "X-Contract-Revision": committed ? "8" : "7" } });
    });
    await page.route("**/api/comfort-schedule/apply-learned", (route) => {
      expect(route.request().headers()["if-match"]).toBe("7");
      posts += 1;
      committed = true;
      post = route; // Store has committed revision 8 before the response is released.
    });
    const accepted = { ...planningContract, revision_id: 8, comfort_schedule: learned };
    await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
      if (route.request().method() === "PUT") { puts += 1; return route.fulfill({ json: accepted }); }
      if (!committed) return route.fulfill({ json: planningContract });
      if (recovered) return route.fulfill({ json: accepted, headers: { "X-Contract-Revision": "8" } });
      refresh = route;
    });
    await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => {
      const revision = route.request().headers()["if-match"];
      previewRevisions.push(revision);
      expect(JSON.parse(route.request().postData()!).comfort_schedule).toEqual(revision === "7" ? draft : learned);
      return route.fulfill({ json: previewBody(Number(revision)) });
    });
    await page.goto("/settings");
    const section = comfortSection(page);
    const apply = section.getByRole("button", { name: "Apply Learned Schedule" });
    const save = section.getByRole("button", { name: "Save Schedule", exact: true });
    await expect(save).toBeEnabled();
    await section.getByRole("checkbox").nth(6).click();
    expect(await scheduleMask(page)).toEqual(expectedMask(draft));
    await section.getByRole("button", { name: "Preview next 24 hours" }).click();
    await expect(section).toContainText("Preview from revision 7");
    await apply.click();
    await expect.poll(() => !!post).toBe(true);
    await page.locator("#setting-quiet_mode_start").fill("21");
    await page.getByRole("button", { name: "Save 1 change", exact: true }).click();
    await expect(page.locator(".override-banner")).toContainText("Saved 1 setting");
    await expect(section).toContainText("Planning target and timezone metadata unavailable");
    await post!.fulfill({
      status: 200, contentType: "application/json", body: scenario.response.body,
      headers: { "X-Contract-Revision": "8" },
    });
    await expect.poll(() => !!refresh).toBe(true);
    await expect(section).toContainText("Learned update outcome is unknown");
    expect(await scheduleMask(page)).toEqual(expectedMask(draft));
    await expect(section).not.toContainText("Applied learned schedule");
    await expect(section).not.toContainText("Preview from revision");
    await expect(apply).toBeDisabled();
    await expect(save).toBeDisabled();
    if (scenario.recovery) {
      await refresh!.fulfill({
        json: { ...accepted, ...scenario.recovery.patch },
        headers: scenario.recovery.name === "missing revision" ? {} : { "X-Contract-Revision": "8" },
      });
      await expect(section).toContainText("Planning settings are incomplete");
      await expect(section).toContainText("Learned update outcome is unknown");
      await expect(section.getByRole("button", { name: "Load saved schedule", exact: true })).toHaveCount(0);
      await expect(apply).toBeDisabled();
      await expect(save).toBeDisabled();
      expect(await scheduleMask(page)).toEqual(expectedMask(draft));
      await apply.dispatchEvent("click");
      await save.dispatchEvent("click");
      recovered = true;
      await section.getByRole("button", { name: "Reload planning settings" }).click();
    } else {
      await refresh!.fulfill({ json: accepted, headers: { "X-Contract-Revision": "8" } });
    }
    await expect(section).toContainText("Saved schedule: weekday 10, 12; weekend 11");
    await expect(section).toContainText("Your unsaved schedule: weekday 6, 7; weekend 8");
    await expect(section).toContainText("Learned update outcome is unknown");
    await expect(section).not.toContainText("Applied learned schedule");
    expect(await scheduleMask(page)).toEqual(expectedMask(draft));
    await expect(apply).toBeDisabled();
    await expect(save).toBeDisabled();
    await apply.dispatchEvent("click");
    await save.dispatchEvent("click");
    expect(posts).toBe(1);
    expect(puts).toBe(0);
    await section.getByRole("button", { name: "Load saved schedule", exact: true }).click();
    await expect(save).toBeEnabled();
    expect(await scheduleMask(page)).toEqual(expectedMask(learned));
    await expect(section).not.toContainText("Learned update outcome is unknown");
    await section.getByRole("button", { name: "Preview next 24 hours" }).click();
    await expect(section).toContainText("Preview from revision 8");
    expect(previewRevisions).toEqual(["7", "8"]);
    expect(puts).toBe(0); // No destructive follow-up is needed to prove the guard.
  });
}

for (const input of ["mouse", "keyboard"] as const) {
  test(`R10-Q02 ${input} initial-load failure preserves edited draft through recovery`, async ({ page }) => {
    await mockSettings(page);
    let recovery: Route | undefined;
    let recovering = false;
    const draft = { weekday: [10, 12], weekend: [] };
    const writes: Array<{ revision: string; schedule: unknown }> = [];
    await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
      if (route.request().method() === "PUT") {
        writes.push({ revision: route.request().headers()["if-match"], schedule: JSON.parse(route.request().postData()!).comfort_schedule });
        return route.fulfill({ json: { ...planningContract, revision_id: 10, comfort_schedule: draft } });
      }
      if (recovering) { recovery = route; return; }
      return route.fulfill({ status: 503, json: { detail: "offline" } });
    });
    await page.goto("/settings");
    const section = comfortSection(page);
    await expect(section).toContainText("Failed to load planning settings");
    const edit = async (hour: number) => {
      const cell = section.getByRole("checkbox").nth(hour);
      if (input === "mouse") await cell.click();
      else { await cell.focus(); await cell.press("Space"); }
    };
    await edit(10);
    recovering = true;
    await section.getByRole("button", { name: "Reload planning settings" }).click();
    await expect.poll(() => !!recovery).toBe(true);
    await edit(12); // A second edit while recovery is pending must also survive.
    expect(await scheduleMask(page)).toEqual(expectedMask(draft));
    await recovery!.fulfill({
      json: { ...planningContract, revision_id: 9, timezone: "Asia/Kolkata", comfort_temp_target: 23 },
      headers: { "X-Contract-Revision": "9" },
    });
    const save = section.getByRole("button", { name: "Save Schedule", exact: true });
    await expect(save).toBeEnabled();
    await expect(section).toContainText("Planner timezone: Asia/Kolkata. Scheduled target: 23 °C");
    expect(await scheduleMask(page)).toEqual(expectedMask(draft));
    await save.click();
    await expect(section).toContainText("Schedule saved");
    expect(writes).toEqual([{ revision: "9", schedule: draft }]);
    expect(await scheduleMask(page)).toEqual(expectedMask(draft));
  });
}

test("R10-Q02 untouched failed initial editor still seeds the saved schedule on recovery", async ({ page }) => {
  await mockSettings(page);
  let recover = false;
  await page.route(`**/api/installations/${installationId}/planning-settings`, (route) =>
    route.fulfill({ status: recover ? 200 : 503, json: planningContract }));
  await page.goto("/settings");
  const section = comfortSection(page);
  await expect(section).toContainText("Failed to load planning settings");
  recover = true;
  await section.getByRole("button", { name: "Reload planning settings" }).click();
  await expect(section.getByRole("button", { name: "Save Schedule" })).toBeEnabled();
  expect(await scheduleMask(page)).toEqual(expectedMask(planningContract.comfort_schedule));
});

for (const mode of ["offline", "network", "incomplete", "mismatched", "malformed"] as const) {
  test(`R9-Q01 repeated learned apply fails closed after ${mode} refresh and recovers draft`, async ({ page }) => {
    await mockSettings(page);
    let posts = 0;
    let puts = 0;
    let refresh: Route | undefined;
    let recovered = false;
    const revisions: string[] = [];
    await page.route("**/api/comfort-schedule/apply-learned", (route) => {
      revisions.push(route.request().headers()["if-match"]);
      posts += 1;
      return route.fulfill({ json: { weekday: [10], weekend: [11] } });
    });
    await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
      if (route.request().method() === "PUT") {
        puts += 1;
        revisions.push(route.request().headers()["if-match"]);
        expect(JSON.parse(route.request().postData()!).comfort_schedule).toEqual({ weekday: [10, 12], weekend: [11] });
        return route.fulfill({ json: { ...planningContract, revision_id: 10 }, headers: { "X-Contract-Revision": "10" } });
      }
      if (recovered) return route.fulfill({
        json: { ...planningContract, revision_id: 9, comfort_schedule: { weekday: [1], weekend: [2] } },
        headers: { "X-Contract-Revision": "9" },
      });
      if (posts) { refresh = route; return; }
      return route.fulfill({ json: planningContract });
    });
    await page.goto("/settings");
    const section = comfortSection(page);
    const apply = section.getByRole("button", { name: "Apply Learned Schedule" });
    const save = section.getByRole("button", { name: "Save Schedule" });
    await apply.click();
    await expect.poll(() => !!refresh).toBe(true);
    await expect(apply).toBeDisabled();
    await expect(section.getByRole("button", { name: "Saving..." })).toBeDisabled();
    // Programmatic dispatch also exercises the handler guard, not only disabled styling.
    await apply.dispatchEvent("click");
    if (mode === "network") await refresh!.abort("failed");
    else if (mode === "malformed") await refresh!.fulfill({ body: "not json" });
    else await refresh!.fulfill({
      status: mode === "offline" ? 503 : 200,
      headers: { "X-Contract-Revision": "9" },
      json: mode === "mismatched" ? { ...planningContract, revision_id: 8 } : {},
    });
    await expect(section).toContainText("Applied learned schedule");
    await expect(apply).toBeDisabled();
    await expect(save).toBeDisabled();
    await apply.dispatchEvent("click");
    await save.dispatchEvent("click");
    expect(posts).toBe(1);
    expect(puts).toBe(0);
    await section.getByRole("checkbox").nth(12).click();
    const draft = await scheduleMask(page);
    recovered = true;
    await section.getByRole("button", { name: "Reload planning settings" }).click();
    await expect(save).toBeEnabled();
    expect(await scheduleMask(page)).toEqual(draft);
    await save.click();
    await expect(section).toContainText("Schedule saved");
    expect(revisions).toEqual(["7", "9"]);
  });
}

for (const writer of ["learned", "schedule"] as const) {
  test(`R9-Q01 ${writer} write uses recovered revision and rejects a concurrent writer`, async ({ page }) => {
    await mockSettings(page);
    let current = 7;
    const writes: string[] = [];
    let held: Route | undefined;
    const path = `**/api/installations/${installationId}/planning-settings`;
    await page.route(path, (route) => {
      if (route.request().method() === "PUT") {
        writes.push(route.request().headers()["if-match"]);
        held = route;
        return;
      }
      return route.fulfill({ json: { ...planningContract, revision_id: current, comfort_schedule: current === 10 && writer === "learned" ? { weekday: [10], weekend: [] } : planningContract.comfort_schedule } });
    });
    await page.route("**/api/comfort-schedule/apply-learned", (route) => {
      writes.push(route.request().headers()["if-match"]);
      held = route;
    });
    await page.goto("/settings");
    const section = comfortSection(page);
    await section.getByRole("checkbox").nth(10).click();
    const draft = await scheduleMask(page);
    await section.getByRole("button", { name: writer === "learned" ? "Apply Learned Schedule" : "Save Schedule" }).click();
    await expect.poll(() => !!held).toBe(true);
    current = 9; // Another writer commits after this request was computed.
    await held!.fulfill({ status: 409, json: { detail: "revision changed" } });
    await expect(section).toContainText("Settings changed elsewhere");
    expect(writes).toEqual(["7"]);
    expect(await scheduleMask(page)).toEqual(draft);
    held = undefined;
    if (writer === "learned") {
      await section.getByRole("button", { name: "Reload planning settings" }).click();
      await expect(section.getByRole("button", { name: "Apply Learned Schedule" })).toBeEnabled();
      await section.getByRole("button", { name: "Apply Learned Schedule" }).click();
    } else {
      await section.getByRole("button", { name: "Save my schedule over this revision" }).click();
    }
    await expect.poll(() => writes.length).toBe(2);
    expect(writes).toEqual(["7", "9"]);
    current = 10; // The successful retry commits before its acknowledgement/GET.
    await held!.fulfill({ json: writer === "learned" ? { weekday: [10], weekend: [] } : { ...planningContract, revision_id: 10 } });
    await expect(section).toContainText(writer === "learned" ? "Applied learned schedule" : "Schedule saved");
  });
}

test("R9-Q01 missing initial revision and incomplete accepted PUT cannot enable unversioned writes", async ({ page }) => {
  await mockSettings(page);
  let valid = false;
  let writes = 0;
  await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
    if (route.request().method() === "PUT") {
      expect(route.request().headers()["if-match"]).toBe("7");
      writes += 1;
      valid = false;
      return route.fulfill({ json: {} });
    }
    return route.fulfill({ json: valid ? planningContract : { ...planningContract, revision_id: undefined } });
  });
  await page.goto("/settings");
  const section = comfortSection(page);
  await expect(section).toContainText("Planning settings are incomplete");
  for (const name of ["Apply Learned Schedule", "Save Schedule"]) {
    await expect(section.getByRole("button", { name })).toBeDisabled();
    await section.getByRole("button", { name }).dispatchEvent("click");
  }
  expect(writes).toBe(0);
  valid = true;
  await section.getByRole("button", { name: "Reload planning settings" }).click();
  await expect(section.getByRole("button", { name: "Save Schedule" })).toBeEnabled();
  await section.getByRole("button", { name: "Save Schedule" }).click();
  await expect(section).toContainText("Schedule saved");
  await expect(section.getByRole("button", { name: "Save Schedule" })).toBeDisabled();
  await expect(section.getByRole("button", { name: "Apply Learned Schedule" })).toBeDisabled();
  expect(writes).toBe(1);
});

for (const field of ["comfort_temp_target", "comfort_temp_min", "timezone"] as const) {
  for (const previewState of ["visible", "deferred"] as const) {
    test(`R9-Q02 parent ${field} accepted save invalidates ${previewState} preview and preserves draft`, async ({ page }) => {
      await mockSettings(page);
      const values = {
        ...settings,
        use_comfort_model: { value: "true", type: "bool", description: "Use comfort model" },
        comfort_temp_target: { value: "20.5", type: "float", description: "Target" },
        comfort_temp_min: { value: "18", type: "float", description: "Minimum" },
        timezone: { value: "UTC", type: "text", description: "Timezone" },
      };
      const newValue = field === "timezone" ? "Asia/Kolkata" : field === "comfort_temp_target" ? "23" : "19";
      let accepted = false;
      let contractRefresh: Route | undefined;
      let oldPreview: Route | undefined;
      const previewRevisions: string[] = [];
      await page.route("**/api/settings", (route) => {
        if (route.request().method() === "PUT") {
          expect(route.request().headers()["if-match"]).toBe("7");
          expect(JSON.parse(route.request().postData()!).settings).toEqual({ [field]: newValue });
          accepted = true;
          values[field].value = newValue;
          return route.fulfill({ json: {}, headers: { "X-Contract-Revision": "9" } });
        }
        return route.fulfill({ json: values, headers: { "X-Installation-ID": installationId, "X-Contract-Revision": accepted ? "9" : "7" } });
      });
      await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
        if (accepted) { contractRefresh = route; return; }
        return route.fulfill({ json: planningContract });
      });
      await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => {
        const revision = route.request().headers()["if-match"];
        previewRevisions.push(revision);
        if (!accepted && previewState === "deferred") { oldPreview = route; return; }
        expect(JSON.parse(route.request().postData()!).comfort_schedule).toEqual({ weekday: [7, 10], weekend: [8] });
        return route.fulfill({ json: previewBody(Number(revision), values.timezone.value) });
      });
      await page.goto("/settings");
      const section = comfortSection(page);
      await expect(section).toContainText("Planner timezone: UTC");
      await section.getByRole("checkbox").nth(10).click();
      const draft = await scheduleMask(page);
      await section.getByRole("button", { name: "Preview next 24 hours" }).click();
      if (previewState === "visible") await expect(section).toContainText("Preview from revision 7");
      else await expect.poll(() => !!oldPreview).toBe(true);
      if (field === "timezone") await page.getByRole("tab", { name: "Data Sources", exact: true }).click();
      if (field === "comfort_temp_target") await page.getByRole("button", { name: "Show advanced" }).click();
      await page.locator(`#setting-${field}`).fill(newValue);
      await page.getByRole("button", { name: "Save 1 change", exact: true }).click();
      await expect.poll(() => !!contractRefresh).toBe(true);
      if (field === "timezone") await page.getByRole("tab", { name: "Optimizer", exact: true }).click();
      await expect(section).not.toContainText("Preview from revision");
      await expect(section).toContainText("Planning target and timezone metadata unavailable");
      await expect(section.getByRole("button", { name: "Preview next 24 hours" })).toBeDisabled();
      await expect(section).not.toContainText("Preview from revision");
      expect(await scheduleMask(page)).toEqual(draft);
      const latest = { ...planningContract, revision_id: 9, [field]: field === "timezone" ? newValue : Number(newValue) };
      await contractRefresh!.fulfill({ json: latest, headers: { "X-Contract-Revision": "9" } });
      await expect(section).toContainText(`Planner timezone: ${latest.timezone}. Scheduled target: ${latest.comfort_temp_target} °C`);
      await expect(section).toContainText(`outside-schedule minimum: ${latest.comfort_temp_min} °C`);
      expect(await scheduleMask(page)).toEqual(draft);
      await section.getByRole("button", { name: "Preview next 24 hours" }).click();
      await expect(section).toContainText(`Preview from revision 9 in ${latest.timezone}`);
      if (oldPreview) {
        await releasePreview(page, oldPreview, previewBody(7, "UTC"));
        await expect(section).toContainText(`Preview from revision 9 in ${latest.timezone}`);
        await expect(section).not.toContainText("Preview from revision 7");
      }
      expect(previewRevisions).toEqual(["7", "9"]);
    });
  }
}

for (const outcome of ["error", "revert", "refresh-failure"] as const) {
  test(`R9-Q02 parent ${outcome} does not falsely advance child metadata`, async ({ page }) => {
    await mockSettings(page);
    let accepted = false;
    let contractGets = 0;
    const values = { ...settings, comfort_temp_min: { value: "18", type: "float", description: "Minimum" } };
    await page.route("**/api/settings", (route) => {
      if (route.request().method() === "PUT") {
        if (outcome === "error") return route.fulfill({ status: 500, json: { detail: "Save rejected" } });
        accepted = true;
        return route.fulfill({ json: {}, headers: { "X-Contract-Revision": "9" } });
      }
      return route.fulfill({ json: values, headers: { "X-Installation-ID": installationId, "X-Contract-Revision": "7" } });
    });
    await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
      contractGets += 1;
      return route.fulfill({ status: accepted ? 503 : 200, json: planningContract });
    });
    await page.goto("/settings");
    const section = comfortSection(page);
    await expect(section).toContainText("Planner timezone: UTC");
    await section.getByRole("checkbox").nth(10).click();
    const draft = await scheduleMask(page);
    await section.getByRole("button", { name: "Preview next 24 hours" }).click();
    await expect(section).toContainText("Preview from revision 7");
    await page.locator("#setting-comfort_temp_min").fill("19");
    if (outcome === "revert") {
      page.once("dialog", (dialog) => dialog.accept());
      await page.getByRole("button", { name: "Discard draft" }).click();
      await expect(page.locator("#setting-comfort_temp_min")).toHaveValue("18");
    } else {
      await page.getByRole("button", { name: "Save 1 change", exact: true }).click();
      await expect(page.locator(".override-banner")).toContainText(outcome === "error" ? "Save rejected" : "Saved 1 setting");
    }
    expect(await scheduleMask(page)).toEqual(draft);
    if (outcome === "refresh-failure") {
      await expect(section).toContainText("Failed to load planning settings");
      await expect(section).toContainText("Planning target and timezone metadata unavailable");
      await expect(section).not.toContainText("Preview from revision");
      await expect(section.getByRole("button", { name: "Save Schedule" })).toBeDisabled();
      expect(contractGets).toBe(2);
    } else {
      await expect(section).toContainText("Preview from revision 7");
      await expect(section).toContainText("Planner timezone: UTC. Scheduled target: 20.5 °C");
      expect(contractGets).toBe(1);
    }
  });
}

for (const mode of ["complete", "offline", "network", "mismatched", "incomplete"] as const) {
  test(`R8-Q01 learned update refresh: ${mode}`, async ({ page }) => {
    await mockSettings(page);
    let applied = false;
    let refresh: Route | undefined;
    let previewRevision: string | undefined;
    const latest = { ...planningContract, revision_id: 9, timezone: "Asia/Kolkata", comfort_temp_target: 23, comfort_schedule: { weekday: [10], weekend: [11] } };
    await page.route("**/api/comfort-schedule/apply-learned", (route) => {
      expect(route.request().headers()["if-match"]).toBe("7");
      applied = true;
      return route.fulfill({ json: { weekday: [10], weekend: [11] } });
    });
    await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
      if (applied) { refresh = route; return; }
      return route.fulfill({ json: planningContract, headers: { "X-Contract-Revision": "7" } });
    });
    await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => {
      previewRevision = route.request().headers()["if-match"];
      return route.fulfill({ json: previewBody(Number(previewRevision), applied ? latest.timezone : "UTC") });
    });
    await page.goto("/settings");
    const section = comfortSection(page);
    const preview = page.getByRole("button", { name: "Preview next 24 hours" });
    await preview.click();
    await expect(section).toContainText("Preview from revision 7");
    await page.getByRole("button", { name: "Apply Learned Schedule" }).click();
    await expect.poll(() => !!refresh).toBe(true);
    await expect(section).not.toContainText("Preview from revision");
    await expect(section).toContainText("Planning target and timezone metadata unavailable");
    await expect(preview).toBeDisabled();
    await expect(page.getByRole("checkbox").nth(10)).toHaveAttribute("aria-checked", "true");
    if (mode === "network") {
      await refresh!.abort("failed");
    } else {
      await refresh!.fulfill({
        status: mode === "offline" ? 503 : 200,
        headers: { "X-Contract-Revision": "9" },
        json: mode === "complete" ? latest : mode === "mismatched" ? { ...latest, revision_id: 8 } : {},
      });
    }
    await expect(section).toContainText("Applied learned schedule");
    await expect(section).not.toContainText("Planner timezone: UTC");
    await expect(section).not.toContainText("Preview from revision");
    if (mode === "complete") {
      await expect(section).toContainText("Planner timezone: Asia/Kolkata. Scheduled target: 23 °C");
      await preview.click();
      await expect(section).toContainText("Preview from revision 9 in Asia/Kolkata");
      expect(previewRevision).toBe("9");
    } else {
      await expect(section).toContainText("Planning target and timezone metadata unavailable");
      await expect(preview).toBeDisabled();
    }
  });
}

for (const failure of ["offline", "network", "incomplete"] as const) {
  for (const recovery of ["same", "overwrite", "load"] as const) {
    test(`QP21-01 versioned learned acknowledgement recovers after ${failure}: ${recovery}`, async ({ page }) => {
      await mockSettings(page);
      const acknowledged = { weekday: [10], weekend: [11] };
      const ackContract = { ...planningContract, revision_id: 8, comfort_schedule: acknowledged };
      // Even identical masks at r9 require explicit comparison: metadata may have changed.
      const recovered = recovery === "same" ? ackContract : { ...ackContract, revision_id: 9, timezone: "Asia/Kolkata", comfort_temp_target: 23 };
      let applied = false;
      let recovering = false;
      let recoveryRoute: Route | undefined;
      const writes: Array<{ revision: string | undefined; schedule: unknown }> = [];
      const previews: Array<{ revision: string | undefined; schedule: unknown }> = [];
      await page.route("**/api/comfort-schedule/apply-learned", (route) => {
        expect(route.request().headers()["if-match"]).toBe("7");
        applied = true;
        return route.fulfill({ json: { ...acknowledged, revision_id: 8 }, headers: { "X-Contract-Revision": "8" } });
      });
      await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
        if (route.request().method() === "PUT") {
          const schedule = JSON.parse(route.request().postData()!).comfort_schedule;
          writes.push({ revision: route.request().headers()["if-match"], schedule });
          return route.fulfill({ json: { ...recovered, comfort_schedule: schedule }, headers: { "X-Contract-Revision": String(recovered.revision_id) } });
        }
        if (!applied) return route.fulfill({ json: planningContract, headers: { "X-Contract-Revision": "7" } });
        if (recovering) { recoveryRoute = route; return; }
        if (failure === "network") return route.abort("failed");
        return route.fulfill({ status: failure === "offline" ? 503 : 200, json: failure === "incomplete" ? { revision_id: 8 } : {}, headers: { "X-Contract-Revision": "8" } });
      });
      await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => {
        previews.push({ revision: route.request().headers()["if-match"], schedule: JSON.parse(route.request().postData()!).comfort_schedule });
        return route.fulfill({ json: previewBody(recovered.revision_id, recovered.timezone) });
      });
      await page.goto("/settings");
      const section = comfortSection(page);
      const save = section.getByRole("button", { name: "Save Schedule", exact: true });
      const apply = section.getByRole("button", { name: "Apply Learned Schedule" });
      const preview = section.getByRole("button", { name: "Preview next 24 hours" });
      await apply.click();
      await expect(section).toContainText("Applied learned schedule");
      await expect.poll(() => scheduleMask(page)).toEqual(expectedMask(acknowledged));
      await expect(save).toBeDisabled();
      await expect(apply).toBeDisabled();
      await expect(preview).toBeDisabled();
      await save.dispatchEvent("click");
      await apply.dispatchEvent("click");
      expect(writes).toEqual([]);
      // Edits both before and during the recovery request must survive.
      await section.getByRole("checkbox").nth(12).click();
      recovering = true;
      await section.getByRole("button", { name: "Reload planning settings", exact: true }).click();
      await expect.poll(() => !!recoveryRoute).toBe(true);
      await section.getByRole("checkbox").nth(13).click();
      const draft = { weekday: [10, 12, 13], weekend: [11] };
      await expect(save).toBeDisabled();
      await expect(preview).toBeDisabled();
      await recoveryRoute!.fulfill({ json: recovered, headers: { "X-Contract-Revision": String(recovered.revision_id) } });
      await expect.poll(() => scheduleMask(page)).toEqual(expectedMask(draft));
      if (recovery === "same") {
        await expect(section).toContainText("Planner timezone: UTC");
        await expect(save).toBeEnabled();
        await expect(apply).toBeEnabled();
        await expect(section.getByText("Saved schedule:", { exact: false })).toHaveCount(0);
        await preview.click();
        await expect(section).toContainText("Preview from revision 8");
        expect(previews).toEqual([{ revision: "8", schedule: draft }]);
        await save.click();
        await expect(section).toContainText("Schedule saved");
        expect(writes).toEqual([{ revision: "8", schedule: draft }]);
      } else {
        await expect(section).toContainText("Saved schedule: weekday 10; weekend 11.");
        await expect(section).toContainText("Your unsaved schedule: weekday 10, 12, 13; weekend 11.");
        await expect(save).toBeDisabled();
        await expect(apply).toBeDisabled();
        await expect(preview).toBeDisabled();
        await save.dispatchEvent("click");
        expect(writes).toEqual([]);
        expect(previews).toEqual([]);
        if (recovery === "overwrite") {
          await section.getByRole("button", { name: "Save my schedule over this revision" }).click();
          await expect(section).toContainText("Schedule saved");
          expect(writes).toEqual([{ revision: "9", schedule: draft }]);
        } else {
          await section.getByRole("button", { name: "Load saved schedule", exact: true }).click();
          await expect.poll(() => scheduleMask(page)).toEqual(expectedMask(acknowledged));
          expect(writes).toEqual([]);
        }
        await expect(section).toContainText("Planner timezone: Asia/Kolkata");
        await expect(save).toBeEnabled();
        await preview.click();
        await expect(section).toContainText("Preview from revision 9");
      }
    });
  }
}

for (const staleStatus of [200, 409, 503]) {
  test(`R8-Q02 newest preview wins over deferred ${staleStatus}`, async ({ page }) => {
    await mockSettings(page);
    const requests: Route[] = [];
    await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => { requests.push(route); });
    await page.goto("/settings");
    const preview = page.getByRole("button", { name: "Preview next 24 hours" });
    const section = comfortSection(page);
    await preview.click();
    await expect.poll(() => requests.length).toBe(1);
    await page.getByRole("checkbox").nth(8).click();
    await preview.click();
    await expect.poll(() => requests.length).toBe(2);
    expect(requests.map((route) => JSON.parse(route.request().postData()!).comfort_schedule.weekday))
      .toEqual([[7], [7, 8]]);
    await releasePreview(page, requests[1], previewBody(7, "latest-draft"));
    await expect(section).toContainText("Preview from revision 7 in latest-draft");
    await releasePreview(page, requests[0], previewBody(7, "stale-draft"), staleStatus);
    await expect(section).toContainText("Preview from revision 7 in latest-draft");
    await expect(section).not.toContainText("stale-draft");
    await expect(section.getByRole("alert")).toHaveCount(0);
  });
}

for (const action of ["edit", "save", "load", "overwrite", "learned"] as const) {
  test(`R8-Q02 deferred preview cannot repopulate after ${action}`, async ({ page }) => {
    await mockSettings(page);
    let heldPreview: Route | undefined;
    let writes = 0;
    let learned = false;
    const latest = { ...planningContract, revision_id: 8, timezone: "Asia/Kolkata", comfort_temp_target: 23, comfort_schedule: { weekday: [10], weekend: [11] } };
    await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => { heldPreview = route; });
    await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
      if (route.request().method() === "PUT") {
        writes += 1;
        if ((action === "load" || action === "overwrite") && writes === 1) {
          return route.fulfill({ status: 409, json: { detail: "changed" } });
        }
        return route.fulfill({ json: latest, headers: { "X-Contract-Revision": "8" } });
      }
      return route.fulfill({
        json: writes || learned ? latest : planningContract,
        headers: { "X-Contract-Revision": writes || learned ? "8" : "7" },
      });
    });
    await page.route("**/api/comfort-schedule/apply-learned", (route) => {
      learned = true;
      return route.fulfill({ json: { weekday: [10], weekend: [11] } });
    });
    await page.goto("/settings");
    const section = comfortSection(page);
    if (action === "load" || action === "overwrite") {
      await page.getByRole("button", { name: "Save Schedule", exact: true }).click();
      await expect(section).toContainText("Saved schedule:");
    }
    await page.getByRole("button", { name: "Preview next 24 hours" }).click();
    await expect.poll(() => !!heldPreview).toBe(true);
    if (action === "edit") {
      await page.getByRole("checkbox").nth(8).click();
    } else if (action === "learned") {
      await page.getByRole("button", { name: "Apply Learned Schedule" }).click();
      await expect(section).toContainText("Applied learned schedule");
    } else if (action === "load") {
      await page.getByRole("button", { name: "Load saved schedule", exact: true }).click();
      await expect(section).not.toContainText("Saved schedule:");
    } else {
      await page.getByRole("button", { name: action === "save" ? "Save Schedule" : "Save my schedule over this revision", exact: true }).click();
      await expect(section).toContainText("Schedule saved");
    }
    await releasePreview(page, heldPreview!, previewBody(7, "stale-response"));
    await expect(section).not.toContainText("Preview from revision");
    await expect(section).not.toContainText("stale-response");
    if (action !== "edit") await expect(section).toContainText("Planner timezone: Asia/Kolkata");
  });
}

test("R8-Q02 existing preview disappears on edit and repeated preview requests are ordered", async ({ page }) => {
  await mockSettings(page);
  await page.goto("/settings");
  const section = comfortSection(page);
  const preview = page.getByRole("button", { name: "Preview next 24 hours" });
  await preview.click();
  await expect(section).toContainText("Preview from revision");
  await page.getByRole("checkbox").nth(8).click();
  await expect(section).not.toContainText("Preview from revision");
  const requests: Route[] = [];
  await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => { requests.push(route); });
  await preview.click();
  await preview.click();
  await expect.poll(() => requests.length).toBe(2);
  await releasePreview(page, requests[1], previewBody(7, "newest"));
  await expect(section).toContainText("Preview from revision 7 in newest");
  await releasePreview(page, requests[0], previewBody(7, "oldest"));
  await expect(section).toContainText("Preview from revision 7 in newest");
});

test("R8-Q02 failed newest preview never falls back to an older success", async ({ page }) => {
  await mockSettings(page);
  const requests: Route[] = [];
  await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => { requests.push(route); });
  await page.goto("/settings");
  const preview = page.getByRole("button", { name: "Preview next 24 hours" });
  const section = comfortSection(page);
  await preview.click();
  await preview.click();
  await expect.poll(() => requests.length).toBe(2);
  await releasePreview(page, requests[1], { detail: "offline" }, 503);
  await expect(section).toContainText("Failed to preview planning settings");
  await releasePreview(page, requests[0], previewBody(7, "old-success"));
  await expect(section).not.toContainText("Preview from revision");
  await expect(section).toContainText("Failed to preview planning settings");
});

test("R8-Q02 accepted save invalidates a preview started while the PUT was pending", async ({ page }) => {
  await mockSettings(page);
  let write: Route | undefined;
  let preview: Route | undefined;
  await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
    if (route.request().method() === "PUT") { write = route; return; }
    return route.fulfill({ json: planningContract, headers: { "X-Contract-Revision": "7" } });
  });
  await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => { preview = route; });
  await page.goto("/settings");
  await page.getByRole("button", { name: "Save Schedule", exact: true }).click();
  await expect.poll(() => !!write).toBe(true);
  await page.getByRole("button", { name: "Preview next 24 hours" }).click();
  await expect.poll(() => !!preview).toBe(true);
  await write!.fulfill({
    json: { ...planningContract, revision_id: 8 },
    headers: { "X-Contract-Revision": "8" },
  });
  const section = comfortSection(page);
  await expect(section).toContainText("Schedule saved");
  await releasePreview(page, preview!, previewBody(7, "pre-save"));
  await expect(section).not.toContainText("Preview from revision");
  await expect(section).toContainText("Schedule saved");
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

for (const { name, body, expected } of [
  { name: "structured invalid values", body: { detail: { invalid_values: { tank_min_temp: "outside controller range" } } }, expected: "tank_min_temp: outside controller range" },
  { name: "string detail", body: { detail: "Settings temporarily unavailable" }, expected: "Settings temporarily unavailable" },
  { name: "structured refusal", body: { detail: { code: "settings_unavailable" } }, expected: "Save failed" },
  { name: "malformed invalid values", body: { detail: { invalid_values: { tank_min_temp: { message: "not renderable" } } } }, expected: "Save failed" },
  { name: "validation array", body: { detail: [{ msg: "Invalid value", loc: ["body", "settings"] }] }, expected: "Save failed" },
]) {
  test(`settings safely renders ${name} without losing the draft`, async ({ page }) => {
    const pageErrors: string[] = [];
    let saved: Record<string, string> | undefined;
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await mockSettings(page, (payload) => { saved = payload; }, { status: 422, body });
    await page.goto("/settings");
    await page.locator("#setting-tank_min_temp").fill("46");
    await page.getByRole("button", { name: "Save 1 change", exact: true }).click();
    await expect(page.locator(".override-banner[role=alert]")).toHaveText(expected);
    await expect(page.locator("#setting-tank_min_temp")).toHaveValue("46");
    await expect(page.getByRole("button", { name: "Save 1 change", exact: true })).toBeEnabled();
    expect(saved).toEqual({ tank_min_temp: "46" });
    expect(pageErrors).toEqual([]);
    await expect(page.locator("body")).not.toContainText("[object Object]");
  });
}

test("settings preserves read-only metadata, explicit observation exit, maintenance and gate controls", async ({ page }) => {
  let saved: Record<string, string> | undefined;
  await mockSettings(page, (payload) => { saved = payload; });
  const metadata = {
    ...settings,
    optimizer_layer: { value: "rules_only", type: "str", description: "Optimizer layer", options: ["rules_only", "auto"], read_only: true },
    tank_max_temp: { ...settings.tank_max_temp, read_only: true },
    ml_maintenance_enabled: { value: "false", type: "bool", description: "Reporting-only model maintenance" },
    seasonal_calibration_auto_exit: { value: "true", type: "bool", description: "Legacy stored auto exit", read_only: true },
    space_heating_behavior_profile: { value: "default", type: "str", description: "Heating behavior profile", options: ["default"] },
    space_heating_gate_on_offset_c: { value: "-2", type: "float", description: "Heating ON offset" },
    space_heating_gate_off_offset_c: { value: "0", type: "float", description: "Heating OFF offset" },
  };
  await page.route("**/api/settings", (route) => route.request().method() === "GET"
    ? route.fulfill({
      contentType: "application/json",
      headers: { "X-Installation-ID": installationId, "X-Contract-Revision": "7" },
      body: JSON.stringify(metadata),
    })
    : route.fallback());
  await page.goto("/settings");
  await expect(page.locator("#setting-optimizer_layer")).toBeDisabled();
  await expect(page.locator("#setting-tank_max_temp")).toBeDisabled();
  await expect(page.getByRole("heading", { name: "Space Heating Gate", exact: true })).toBeVisible();
  await page.getByLabel("Model maintenance", { exact: true }).check();
  await page.getByLabel("Gate ON offset", { exact: true }).fill("-3");
  await page.getByRole("button", { name: "Show advanced" }).click();
  await expect(page.getByLabel("Automatic observation exit", { exact: true })).toBeDisabled();
  await expect(page.getByLabel("Automatic observation exit", { exact: true })).not.toBeChecked();
  await expect(page.locator("#seasonal-learning")).toContainText("Observation persists until explicit human exit");
  await expect(page.locator("#seasonal-learning")).not.toContainText("end itself");
  await page.getByRole("button", { name: "Save 2 changes", exact: true }).click();
  expect(saved).toEqual({ ml_maintenance_enabled: "true", space_heating_gate_on_offset_c: "-3" });
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

test("settings retains the draft after an API save failure", async ({ page }) => {
  await mockSettings(page, undefined, { status: 500, body: { detail: "Backend unavailable" } });
  await page.goto("/settings?tab=data");
  const input = page.locator("#setting-manual_price_eur_per_kwh");
  await input.fill("0.20");

  await page.getByRole("button", { name: /Save 1 change/ }).click();

  await expect(page.locator(".override-banner[role=alert]")).toContainText("Backend unavailable");
  await expect(input).toHaveValue("0.20");
  await expect(page.getByRole("button", { name: /Save 1 change/ })).toBeEnabled();
});

test("FS-05 successful save preserves edits made while the save request is pending", async ({ page }) => {
  await mockSettings(page);
  await page.unroute("**/api/settings");

  const latest = {
    ...settings,
    manual_price_eur_per_kwh: { ...settings.manual_price_eur_per_kwh, value: "0.20" },
  };
  let pendingSave: Route | undefined;
  await page.route("**/api/settings", (route) => {
    if (route.request().method() === "PUT") {
      expect(route.request().headers()["if-match"]).toBe("7");
      expect(JSON.parse(route.request().postData()!).settings).toEqual({ manual_price_eur_per_kwh: "0.20" });
      pendingSave = route;
      return;
    }
    return route.fulfill({
      status: 200,
      headers: {
        "X-Contract-Revision": pendingSave ? "8" : "7",
        "X-Installation-ID": installationId,
      },
      contentType: "application/json",
      body: JSON.stringify(pendingSave ? latest : settings),
    });
  });

  await page.goto("/settings?tab=data");
  const input = page.locator("#setting-manual_price_eur_per_kwh");
  await input.fill("0.20");
  const saveButton = page.getByRole("button", { name: /Save .*change/ });
  await expect(saveButton).toBeEnabled();
  await saveButton.click();
  await expect.poll(() => Boolean(pendingSave)).toBe(true);
  await input.fill("0.25");
  await pendingSave!.fulfill({ status: 200, headers: { "X-Contract-Revision": "8" }, contentType: "application/json", body: JSON.stringify({ status: "updated", count: 1 }) });
  await expect(input).toHaveValue("0.25");
  await expect(page.getByRole("button", { name: /Save .*change/ })).toBeEnabled();
  await expect(page.getByRole("alert").filter({ hasText: "Current saved planning settings differ" })).toHaveCount(0);
});

test("settings keeps the draft after a revision conflict and retries against the shown revision", async ({ page }) => {
  await mockSettings(page);
  await page.unroute("**/api/settings");

  const latest = {
    ...settings,
    manual_price_eur_per_kwh: { ...settings.manual_price_eur_per_kwh, value: "0.14" },
  };
  const seenRevisions: (string | undefined)[] = [];
  let visibleRevision = "7";
  await page.route("**/api/settings", (route) => {
    if (route.request().method() !== "PUT") {
      return route.fulfill({
        status: 200,
        headers: {
          "X-Contract-Revision": visibleRevision,
          "X-Installation-ID": installationId,
        },
        contentType: "application/json",
        body: JSON.stringify(latest),
      });
    }

    seenRevisions.push(route.request().headers()["if-match"]);
    if (seenRevisions.length === 1) {
      visibleRevision = "8";
      return route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: "planning settings changed; reload before saving" }),
      });
    }
    return route.fulfill({
      status: 200,
      headers: { "X-Contract-Revision": "9" },
      contentType: "application/json",
      body: JSON.stringify(latest),
    });
  });

  await page.goto("/settings?tab=data");
  const input = page.locator("#setting-manual_price_eur_per_kwh");
  await input.fill("0.20");
  await page.getByRole("button", { name: /Save 1 change/ }).click();

  await expect(
    page.getByRole("alert").filter({ hasText: "Settings changed elsewhere" }),
  ).toBeVisible();
  await expect(input).toHaveValue("0.20");
  await page.getByRole("button", { name: "Save my draft over this revision" }).click();

  await expect.poll(() => seenRevisions).toEqual(["7", "8"]);
  await expect(page.locator(".override-banner[role=status]")).toContainText("Saved 1 setting");
});

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
  const cells = page.getByRole("checkbox");
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
  const cells = page.getByRole("checkbox");

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
  const cells = page.getByRole("checkbox");

  await cells.nth(30).click();

  await expect(cells.nth(30)).toBeFocused();
  await expect(page.locator('[role="checkbox"][tabindex="0"]')).toHaveCount(1);
  await expect(cells.nth(30)).toHaveAttribute("tabindex", "0");
});

test("comfort schedule clamps learned hours before saving", async ({ page }) => {
  let savedContract: unknown;
  let applied = false;
  await mockSettings(page);
  await page.route("**/api/comfort-schedule/apply-learned", (route) => {
    applied = true;
    return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ weekday: [-1, 0, 23, 24], weekend: [25] }),
  }); });
  await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
    if (route.request().method() === "PUT") {
      savedContract = JSON.parse(route.request().postData() || "{}");
      return route.fulfill({
        status: 200,
        headers: { ETag: '"8"', "X-Contract-Revision": "8" },
        contentType: "application/json",
        body: JSON.stringify({ ...planningContract, revision_id: 8 }),
      });
    }
    return route.fulfill({
      status: 200,
      headers: { ETag: '"7"', "X-Contract-Revision": "7" },
      contentType: "application/json",
      body: JSON.stringify(applied ? { ...planningContract, comfort_schedule: { weekday: [0, 23], weekend: [] } } : planningContract),
    });
  });
  await page.goto("/settings");

  const applyLearned = page.getByRole("button", { name: "Apply Learned Schedule" });
  await applyLearned.click();
  await expect(applyLearned).toBeFocused();
  await page.getByRole("button", { name: "Save Schedule" }).click();
  await expect.poll(() => savedContract).toEqual({
    comfort_schedule: { weekday: [0, 23], weekend: [] },
  });
});

test("comfort schedule preserves its draft across a revision conflict", async ({ page }) => {
  const writes: Array<{ revision: string | undefined; schedule: unknown }> = [];
  await mockSettings(page);
  await page.unroute(`**/api/installations/${installationId}/planning-settings`);
  await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
    if (route.request().method() === "PUT") {
      writes.push({
        revision: route.request().headers()["if-match"],
        schedule: JSON.parse(route.request().postData() || "{}").comfort_schedule,
      });
      if (writes.length === 1) {
        return route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({ detail: { reason: "contract_revision_changed", current_revision: 8 } }),
        });
      }
      return route.fulfill({
        status: 200,
        headers: { ETag: '"9"', "X-Contract-Revision": "9" },
        contentType: "application/json",
        body: JSON.stringify({ ...planningContract, revision_id: 9 }),
      });
    }
    const latest = writes.length ? {
      ...planningContract,
      comfort_schedule: { weekday: [6], weekend: [9] },
      revision_id: 8,
    } : planningContract;
    return route.fulfill({
      status: 200,
      headers: { ETag: `"${latest.revision_id}"`, "X-Contract-Revision": String(latest.revision_id) },
      contentType: "application/json",
      body: JSON.stringify(latest),
    });
  });
  await page.goto("/settings");

  const cells = page.getByRole("checkbox");
  await cells.nth(8).click();
  await page.getByRole("button", { name: "Save Schedule" }).click();

  const conflict = page.locator(".plan-section [role='alert']").filter({ hasText: "Saved schedule:" });
  await expect(conflict).toContainText("Saved schedule: weekday 6; weekend 9.");
  await expect(conflict).toContainText("Your unsaved schedule: weekday 7, 8; weekend 8.");
  await expect(cells.nth(8)).toHaveAttribute("aria-checked", "true");

  await page.getByRole("button", { name: "Save my schedule over this revision" }).click();
  await expect.poll(() => writes).toEqual([
    { revision: "7", schedule: { weekday: [7, 8], weekend: [8] } },
    { revision: "8", schedule: { weekday: [7, 8], weekend: [8] } },
  ]);
  await expect(
    page.locator(".plan-section [role='status']").filter({ hasText: "Schedule saved" }),
  ).toContainText("Schedule saved");
});

test("comfort schedule preview uses the canonical installation contract and renders its targets", async ({ page }) => {
  let previewRequest: { body: Record<string, unknown>; ifMatch: string | null } | undefined;
  await mockSettings(page);
  await page.route(
    `**/api/installations/${installationId}/planning-settings/preview`,
    async (route) => {
      previewRequest = {
        body: JSON.parse(route.request().postData() || "{}"),
        ifMatch: route.request().headers()["if-match"] ?? null,
      };
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          target_schedule: [
            { comfort_hour: true, target: 20.5, local_start: "2026-01-05T07:00", local_end: "2026-01-05T08:00", interval_start_utc: "2026-01-05T07:00Z", interval_end_utc: "2026-01-05T08:00Z" },
            { comfort_hour: false, target: 18, local_start: "2026-01-05T08:00", local_end: "2026-01-05T09:00", interval_start_utc: "2026-01-05T08:00Z", interval_end_utc: "2026-01-05T09:00Z" },
          ],
          base_revision_id: 7,
          proposed_contract: { timezone: "UTC" },
        }),
      });
    },
  );
  await page.goto("/settings");
  await page.getByRole("button", { name: "Preview next 24 hours" }).click();

  await expect.poll(() => previewRequest?.ifMatch).toBe("7");
  expect(previewRequest?.body.comfort_schedule).toEqual(planningContract.comfort_schedule);
  await expect(page.getByRole("status")).toContainText("Preview from revision 7 in UTC");
  await expect(page.getByRole("status")).toContainText("20.5 °C (comfort)");
  await expect(page.getByRole("status")).toContainText("18 °C (setback)");
  await expect(page.getByRole("status")).toContainText("1 scheduled hours.");
});

for (const choice of ["load", "overwrite"] as const) {
  test(`R7-Q01 ${choice} accepts complete metadata after a target/timezone-only conflict`, async ({ page }) => {
    await mockSettings(page);
    const latest = {
      ...planningContract, revision_id: 8, timezone: "Asia/Kolkata",
      comfort_temp_target: 22, comfort_temp_min: 19, comfort_temp_max: 26,
    };
    const writes: Array<{ revision: string; schedule: unknown }> = [];
    let releaseRefresh: (() => void) | undefined;
    const refreshGate = new Promise<void>((resolve) => { releaseRefresh = resolve; });
    let refreshing = false;
    await page.route(`**/api/installations/${installationId}/planning-settings`, async (route) => {
      if (route.request().method() === "PUT") {
        writes.push({
          revision: route.request().headers()["if-match"],
          schedule: JSON.parse(route.request().postData() || "{}").comfort_schedule,
        });
        return route.fulfill(writes.length === 1 ? {
          status: 409, json: { detail: "changed" },
        } : {
          json: { ...latest, revision_id: 9 }, headers: { "X-Contract-Revision": "9" },
        });
      }
      if (writes.length) {
        refreshing = true;
        await refreshGate;
      }
      const contract = writes.length ? latest : planningContract;
      return route.fulfill({ json: contract, headers: { "X-Contract-Revision": String(contract.revision_id) } });
    });
    let previewRevision: string | undefined;
    await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => {
      previewRevision = route.request().headers()["if-match"];
      return route.fulfill({ json: {
        target_schedule: [], base_revision_id: Number(previewRevision),
        proposed_contract: { timezone: latest.timezone },
      } });
    });
    await page.goto("/settings");
    const cells = page.getByRole("checkbox");
    const section = page.locator(".plan-section").filter({ has: page.getByRole("heading", { name: "Comfort Schedule", exact: true }) });
    await expect(section).toContainText("Planner timezone: UTC. Scheduled target: 20.5 °C");
    await cells.nth(8).click();
    await page.getByRole("button", { name: "Save Schedule", exact: true }).click();
    await expect.poll(() => refreshing).toBe(true);
    // Fetching and comparing the new contract must not replace the local draft.
    await expect(cells.nth(8)).toHaveAttribute("aria-checked", "true");
    releaseRefresh!();
    const conflict = section.getByRole("alert").filter({ hasText: "Saved schedule:" });
    await expect(conflict).toContainText("Saved schedule: weekday 7; weekend 8.");
    await expect(conflict).toContainText("Your unsaved schedule: weekday 7, 8; weekend 8.");
    await expect(cells.nth(8)).toHaveAttribute("aria-checked", "true");
    await page.getByRole("button", { name: choice === "load" ? "Load saved schedule" : "Save my schedule over this revision", exact: true }).click();
    await expect(section).toContainText("Planner timezone: Asia/Kolkata. Scheduled target: 22 °C; outside-schedule minimum: 19 °C; outer maximum: 26 °C.");
    await expect(cells.nth(8)).toHaveAttribute("aria-checked", choice === "load" ? "false" : "true");
    await expect(conflict).toHaveCount(0);
    if (choice === "overwrite") {
      await expect(section.getByRole("status")).toContainText("Schedule saved");
      expect(writes).toEqual([
        { revision: "7", schedule: { weekday: [7, 8], weekend: [8] } },
        { revision: "8", schedule: { weekday: [7, 8], weekend: [8] } },
      ]);
    } else {
      expect(writes).toHaveLength(1);
    }
    await page.getByRole("button", { name: "Preview next 24 hours" }).click();
    await expect.poll(() => previewRevision).toBe(choice === "load" ? "8" : "9");
  });
}

for (const mode of ["complete", "refresh", "mismatched", "unavailable"] as const) {
  test(`R7-Q01 successful save metadata: ${mode}`, async ({ page }) => {
    await mockSettings(page);
    let saved = false;
    let refreshes = 0;
    const accepted = { ...planningContract, revision_id: 8, timezone: "Australia/Lord_Howe", comfort_temp_target: 23 };
    await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
      if (route.request().method() === "PUT") {
        saved = true;
        return route.fulfill({
          json: mode === "complete" ? accepted : mode === "mismatched" ? planningContract : {},
          headers: { "X-Contract-Revision": "8" },
        });
      }
      if (saved) {
        refreshes += 1;
        return route.fulfill(mode === "unavailable"
          ? { status: 503, json: { detail: "offline" } }
          : { json: accepted, headers: { "X-Contract-Revision": "8" } });
      }
      return route.fulfill({ json: planningContract, headers: { "X-Contract-Revision": "7" } });
    });
    await page.goto("/settings");
    const cells = page.getByRole("checkbox");
    await expect(cells.nth(7)).toHaveAttribute("aria-checked", "true");
    await cells.nth(8).click();
    await page.getByRole("button", { name: "Save Schedule", exact: true }).click();
    const section = page.locator(".plan-section").filter({ has: page.getByRole("heading", { name: "Comfort Schedule", exact: true }) });
    await expect(section.getByRole("status")).toContainText("Schedule saved");
    await expect(cells.nth(8)).toHaveAttribute("aria-checked", "true");
    expect(refreshes).toBe(mode === "complete" ? 0 : 1);
    if (mode === "unavailable") {
      await expect(section).toContainText("Planning target and timezone metadata unavailable");
      await expect(section).not.toContainText("Planner timezone: UTC");
      await expect(section).not.toContainText("Scheduled target: 20.5");
    } else {
      await expect(section).toContainText("Planner timezone: Australia/Lord_Howe. Scheduled target: 23 °C");
    }
  });
}

for (const scenario of [
  {
    name: "Kolkata one local hour split at a UTC boundary", timezone: "Asia/Kolkata", hours: 1, scheduleHour: 7,
    now: "2026-10-18T23:00:00Z",
    intervals: [
      ["2026-10-19T01:30:00Z", "2026-10-19T02:00:00Z", "2026-10-19T07:00:00+05:30", "2026-10-19T07:30:00+05:30"],
      ["2026-10-19T02:00:00Z", "2026-10-19T02:30:00Z", "2026-10-19T07:30:00+05:30", "2026-10-19T08:00:00+05:30"],
    ],
  },
  {
    name: "Lord Howe repeated hour at half-hour fallback", timezone: "Australia/Lord_Howe", hours: 1.5, scheduleHour: 1,
    now: "2026-04-04T12:00:00Z",
    intervals: [
      ["2026-04-04T14:00:00Z", "2026-04-04T15:00:00Z", "2026-04-05T01:00:00+11:00", "2026-04-05T01:30:00+10:30"],
      ["2026-04-04T15:00:00Z", "2026-04-04T15:30:00Z", "2026-04-05T01:30:00+10:30", "2026-04-05T02:00:00+10:30"],
    ],
  },
  {
    name: "Lord Howe shortened hour at half-hour spring forward", timezone: "Australia/Lord_Howe", hours: 0.5, scheduleHour: 2,
    now: "2026-10-03T12:00:00Z",
    intervals: [
      ["2026-10-03T15:30:00Z", "2026-10-03T16:00:00Z", "2026-10-04T02:30:00+11:00", "2026-10-04T03:00:00+11:00"],
    ],
  },
  {
    name: "clipped comfort interval", timezone: "Asia/Kolkata", hours: 0.25, scheduleHour: 5,
    now: "2026-10-18T23:00:00Z",
    intervals: [
      ["2026-10-19T00:00:00Z", "2026-10-19T00:15:00Z", "2026-10-19T05:30:00+05:30", "2026-10-19T05:45:00+05:30"],
    ],
  },
]) {
  test(`R7-Q02 preview duration: ${scenario.name}`, async ({ page }) => {
    await page.clock.setFixedTime(new Date(scenario.now));
    await mockSettings(page);
    const selectedSchedule = { weekday: [scenario.scheduleHour], weekend: [scenario.scheduleHour] };
    await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => route.fulfill({
      json: { ...planningContract, timezone: scenario.timezone, comfort_schedule: selectedSchedule },
      headers: { "X-Contract-Revision": "7" },
    }));
    let requestedSchedule: unknown;
    await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => {
      requestedSchedule = JSON.parse(route.request().postData() || "{}").comfort_schedule;
      const intervals = scenario.intervals.map(([start, end, localStart, localEnd]) => ({
        interval_start_utc: start, interval_end_utc: end,
        local_start: localStart, local_end: localEnd, comfort_hour: true, target: 20.5,
      }));
      return route.fulfill({ json: {
        target_schedule: intervals,
        base_revision_id: 7, proposed_contract: { timezone: scenario.timezone },
      } });
    });
    await page.goto("/settings");
    await page.getByRole("button", { name: "Preview next 24 hours" }).click();
    await expect(page.getByRole("status")).toContainText(`Preview from revision 7 in ${scenario.timezone}; ${scenario.hours} scheduled hours.`);
    expect(requestedSchedule).toEqual(selectedSchedule);
  });
}

test("AC3 learned POST-r8 GET-r9 preserves acknowledged schedule until explicit comparison", async ({ page }) => {
  await mockSettings(page);
  const acknowledged = { weekday: [10], weekend: [11] };
  const competing = { weekday: [14], weekend: [15] };
  let applied = false;
  const writes: string[] = [];
  await page.route("**/api/comfort-schedule/apply-learned", (route) => {
    expect(route.request().headers()["if-match"]).toBe("7");
    applied = true;
    return route.fulfill({ json: { ...acknowledged, revision_id: 8 }, headers: { "X-Contract-Revision": "8" } });
  });
  await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
    if (route.request().method() === "PUT") {
      writes.push(route.request().headers()["if-match"]);
      expect(JSON.parse(route.request().postData()!).comfort_schedule).toEqual(acknowledged);
      return route.fulfill({ json: { ...planningContract, revision_id: 10, comfort_schedule: acknowledged } });
    }
    return route.fulfill({ json: applied ? { ...planningContract, revision_id: 9, comfort_schedule: competing } : planningContract });
  });
  await page.goto("/settings");
  const section = comfortSection(page);
  await expect(section).toContainText("Planner timezone: UTC");
  await section.getByRole("button", { name: "Apply Learned Schedule" }).click();
  await expect(section).toContainText("acknowledged at revision 8");
  expect(await scheduleMask(page)).toEqual(expectedMask(acknowledged));
  await expect(section).toContainText("Saved schedule: weekday 14; weekend 15");
  await expect(section.getByRole("button", { name: "Save Schedule", exact: true })).toBeDisabled();
  await expect(section.getByRole("button", { name: "Preview next 24 hours" })).toBeDisabled();
  await section.getByRole("button", { name: "Save Schedule", exact: true }).dispatchEvent("click");
  expect(writes).toEqual([]);
  await section.getByRole("button", { name: "Save my schedule over this revision" }).click();
  await expect(section).toContainText("Schedule saved");
  expect(writes).toEqual(["9"]);
  expect(await scheduleMask(page)).toEqual(expectedMask(acknowledged));
});
// Independent affected-gate coverage: failed retries must not discard the POST anchor.
for (const recovery of ["same", "changed-mask", "competing"] as const) {
  test(`QP21-01 independent repeated retry preserves acknowledgement: ${recovery}`, async ({ page }) => {
    await mockSettings(page);
    const acknowledged = { weekday: [10], weekend: [11] };
    const saved = recovery === "same" ? acknowledged : { weekday: [14], weekend: [15] };
    const revision = recovery === "competing" ? 9 : 8;
    const recovered = { ...planningContract, revision_id: revision, comfort_schedule: saved };
    let applied = false;
    let posts = 0;
    let writes = 0;
    let previews = 0;
    const retries: Route[] = [];
    let retrying = false;
    await page.route("**/api/comfort-schedule/apply-learned", (route) => {
      expect(route.request().headers()["if-match"]).toBe("7");
      posts += 1;
      applied = true;
      return route.fulfill({ json: { ...acknowledged, revision_id: 8 }, headers: { "X-Contract-Revision": "8" } });
    });
    await page.route(`**/api/installations/${installationId}/planning-settings`, (route) => {
      if (route.request().method() !== "GET") { writes += 1; return route.fulfill({ status: 500, json: {} }); }
      if (!applied) return route.fulfill({ json: planningContract, headers: { "X-Contract-Revision": "7" } });
      if (retrying) { retries.push(route); return; }
      return route.fulfill({ status: 503, json: {} });
    });
    await page.route(`**/api/installations/${installationId}/planning-settings/preview`, (route) => {
      previews += 1;
      expect(route.request().headers()["if-match"]).toBe(String(revision));
      expect(JSON.parse(route.request().postData()!).comfort_schedule).toEqual(recovery === "same" ? draft : saved);
      return route.fulfill({ json: previewBody(revision) });
    });
    const draft = { weekday: [10, 12, 13, 16], weekend: [11] };
    await page.goto("/settings");
    const section = comfortSection(page);
    const apply = section.getByRole("button", { name: "Apply Learned Schedule" });
    const save = section.getByRole("button", { name: "Save Schedule", exact: true });
    const preview = section.getByRole("button", { name: "Preview next 24 hours" });
    const reload = section.getByRole("button", { name: "Reload planning settings", exact: true });
    await apply.click();
    await expect(section).toContainText("Applied learned schedule");
    await expect.poll(() => scheduleMask(page)).toEqual(expectedMask(acknowledged));
    retrying = true;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await expect(reload).toBeEnabled();
      await reload.click();
      await expect.poll(() => retries.length).toBe(attempt + 1);
      await expect(reload).toBeDisabled();
      await section.getByRole("checkbox").nth([12, 13, 16][attempt]).click();
      await expect(save).toBeDisabled();
      await expect(apply).toBeDisabled();
      await expect(preview).toBeDisabled();
      await save.dispatchEvent("click");
      await apply.dispatchEvent("click");
      expect(posts).toBe(1);
      expect(writes).toBe(0);
      expect(previews).toBe(0);
      if (attempt === 0) await retries[attempt].abort("failed");
      else if (attempt === 1) {
        // Valid-looking metadata with disagreeing header/body revisions is not recovery.
        await retries[attempt].fulfill({ json: { ...recovered, revision_id: 8 }, headers: { "X-Contract-Revision": "9" } });
      } else await retries[attempt].fulfill({ json: recovered, headers: { "X-Contract-Revision": String(revision) } });
    }
    await expect.poll(() => scheduleMask(page)).toEqual(expectedMask(draft));
    if (recovery !== "same") {
      await expect(section).toContainText("Saved schedule: weekday 14; weekend 15.");
      await expect(section).toContainText("Your unsaved schedule: weekday 10, 12, 13, 16; weekend 11.");
      await expect(save).toBeDisabled();
      await expect(apply).toBeDisabled();
      await expect(preview).toBeDisabled();
      await section.getByRole("button", { name: "Load saved schedule", exact: true }).click();
      await expect.poll(() => scheduleMask(page)).toEqual(expectedMask(saved));
    }
    await expect(save).toBeEnabled();
    await expect(apply).toBeEnabled();
    await preview.click();
    await expect(section).toContainText(`Preview from revision ${revision}`);
    expect(posts).toBe(1);
    expect(writes).toBe(0);
    expect(previews).toBe(1);
  });
}