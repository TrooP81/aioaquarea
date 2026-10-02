import { expect, test } from "./fixtures";

const now = Date.now();
const iso = (offsetHours: number) => new Date(now + offsetHours * 3_600_000).toISOString();

async function timelineFixture(page: import("@playwright/test").Page) {
    await page.route("**/api/prices?hours=48", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify([{ ts: iso(-1), price_eur_per_kwh: 0.12 }, { ts: iso(1), price_eur_per_kwh: 0.42 }]) }));
    await page.route("**/api/indoor-temp?hours=24", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify([{ timestamp: iso(-1), temperature: 20.2 }]) }));
    await page.route("**/api/thermal/indoor-forecast?hours=24", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ forecast_with_plan: [{ hour: 1, ts: iso(1), predicted_indoor_temp: 20.6 }], forecast_no_heating: [], target_schedule: [{ hour: 1, target: 21, comfort_hour: true }], planned_actions: [], weather_forecast: [], price_forecast: [], forecast_status: "available" }) }));
    await page.route("**/api/settings", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ comfort_temp_min: { value: "20" }, comfort_temp_max: { value: "22" } }) }));
    await page.route("**/api/plan-activity?*", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify([{ id: 8, plan_id: 4, plan_created_at: iso(-2), optimizer_version: "rules_v7", scheduled_ts: iso(1), action_type: "zone_temp_boost", status: "skipped_peak", executed_at: null, payload: {}, result: { reason: "price_peak" } }]) }));
    await page.route("**/api/currency", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ code: "EUR", prefix: "EUR ", suffix: "", multiplier: 100, price_label: "EURc/kWh" }) }));
}

test("timeline has a fixed domain, visible Now marker, series and semantic table", async ({ page }) => {
    await timelineFixture(page);
    await page.goto("/?view=timeline");
    await expect(page.getByTestId("explanation-timeline")).toBeVisible();
    await expect(page.getByText("Past 24 hours of readings and outcomes, followed by the next 24 hours of plan and forecast.")).toBeVisible();
    await expect(page.locator(".timeline-now span")).toHaveText("Now");
    await expect(page.locator(".timeline-axis span")).toHaveCount(3);
    await expect(page.locator(".timeline-price-band span")).toHaveCount(2);
    await expect(page.locator(".timeline-actual")).toHaveCount(1);
    await expect(page.locator(".timeline-forecast")).toHaveCount(1);
    await expect(page.locator(".timeline-comfort-band")).toHaveText("Comfort band");
    await expect(page.getByRole("table")).toBeVisible();
});

test("timeline uses configured price presentation and exposes marker reasons to keyboard users", async ({ page }) => {
    await timelineFixture(page);
    await page.goto("/?view=timeline");
    await expect(page.getByText("Price (EURc/kWh)")).toBeVisible();
    const marker = page.getByRole("button", { name: /Boost heating, Skipped \(peak price\).*price peak/i });
    await marker.focus();
    await expect(marker).toBeFocused();
    await expect(page.getByText("Skipped (peak price): price peak")).toBeVisible();
});

test("timeline preserves action status vocabulary and table parity", async ({ page }) => {
    await timelineFixture(page);
    const statuses = [
        ["executed", "Done"],
        ["executed_unverified", "Done (unverified)"],
        ["failed", "Failed"],
        ["expired", "Missed"],
        ["skipped", "Skipped"],
        ["skipped_peak", "Skipped (peak price)"],
        ["cancelled", "Cancelled"],
    ] as const;
    await page.route("**/api/plan-activity?*", (route) => route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(statuses.map(([status], index) => ({
            id: index + 10,
            plan_id: 4,
            plan_created_at: iso(-2),
            optimizer_version: "rules_v7",
            scheduled_ts: iso(status === "skipped_peak" ? 1 : index + 2),
            action_type: "zone_temp_boost",
            status,
            executed_at: null,
            payload: {},
            result: status === "executed" ? { verified: true } : { reason: status === "skipped_peak" ? "price_peak" : status },
        }))),
    }));
    await page.goto("/?view=timeline");
    const activity = page.getByTestId("plan-activity");
    for (const [, label] of statuses) await expect(activity.locator(".plan-action-status").getByText(label, { exact: true })).toBeVisible();

    const table = page.getByRole("table");
    for (const heading of ["Time", "Actual", "Forecast", "Comfort range", "Target", "Price", "Action", "Status", "Reason"]) {
        await expect(table.getByRole("columnheader", { name: heading, exact: true })).toBeVisible();
    }
    await expect(table).toContainText("Boost heating");
    await expect(table).toContainText("Skipped (peak price)");
    await expect(table).toContainText("price peak");
});

test("timeline leaves available sources visible when another source fails", async ({ page }) => {
    await timelineFixture(page);
    await page.route("**/api/indoor-temp?hours=24", (route) => route.fulfill({ status: 500 }));
    await page.goto("/?view=timeline");
    await expect(page.getByText(/Unavailable: indoor unavailable/)).toBeVisible();
    await expect(page.getByTestId("explanation-timeline")).toBeVisible();
});

test("timeline preserves forecast comfort-risk messaging", async ({ page }) => {
    await timelineFixture(page);
    await page.route("**/api/thermal/indoor-forecast?hours=24", (route) => route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
            forecast_with_plan: [{ hour: 1, ts: iso(1), predicted_indoor_temp: 19.2 }],
            forecast_no_heating: [],
            target_schedule: [{ hour: 1, target: 21, comfort_hour: true }],
            planned_actions: [],
            weather_forecast: [],
            price_forecast: [],
            forecast_status: "available",
            comfort_assessment: {
                state: "at_risk",
                summary: "The room is expected to miss the comfort target.",
                first_miss: { ts: iso(1), predicted_c: 19.2, target_c: 21, shortfall_c: 1.8 },
                worst_miss: { shortfall_c: 2.1 },
                controllability: { status: "mode_only_no_space_heat", cutoff_c: 15 },
                recommendations: [{ title: "Lower heat cutoff", setting_key: "controller_heat_cutoff", current_value_c: 15, minimum_candidate_value_c: 17, confidence: "medium" }],
            },
        }),
    }));
    await page.goto("/?view=timeline");
    const riskPanel = page.locator("#dashboard-panel-timeline .comfort-risk-card");
    await expect(riskPanel).toContainText(/Comfort risk/);
    await expect(riskPanel).toContainText("The room is expected to miss the comfort target.");
    await expect(riskPanel).toContainText(/Worst shortfall: 2.1°C below target/);
    await expect(riskPanel).toContainText(/Controllability: mode only no space heat/);
    await expect(riskPanel.getByRole("link", { name: "Review heat cutoff" })).toHaveAttribute("href", "/settings?tab=optimizer#controller-heat-curve");
});

test("timeline uses exact fixed 48-hour domain endpoints", async ({ page }) => {
    const fixedNow = Date.parse("2026-10-02T12:00:00.000Z");
    await page.addInitScript(({ fixedNow }) => {
        Date.now = () => fixedNow;
    }, { fixedNow });
    await timelineFixture(page);
    await page.goto("/?view=timeline");
    const timeline = page.getByTestId("explanation-timeline");
    await expect(timeline).toHaveAttribute("data-domain-start", "2026-10-01T12:00:00.000Z");
    await expect(timeline).toHaveAttribute("data-domain-end", "2026-10-03T12:00:00.000Z");
});

for (const width of [375, 640]) {
    test(`timeline keeps page width within the ${width}px viewport`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await timelineFixture(page);
        await page.goto("/?view=timeline");
        await expect(page.getByTestId("explanation-timeline")).toBeVisible();
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1);
    });
}

test("timeline risk panel agrees with control state", async ({ page }) => {
    await timelineFixture(page);
    await page.route("**/api/control-state", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ state: "comfort_at_risk", headline: "Comfort needs attention", detail: "Control state reports a comfort risk.", reason_code: "comfort_at_risk", since: null, until: null, override_id: null, active_override_count: 0, primary_action: null, notices: [], resolved_at: iso(0) }) }));
    await page.goto("/?view=timeline");
    await expect(page.locator(".comfort-risk-card")).toContainText("Comfort risk");
    await expect(page.locator(".comfort-risk-card")).toContainText("Control state reports a comfort risk.");
});

test("timeline keeps forecast and control-state comfort risk messaging consistent", async ({ page }) => {
    await timelineFixture(page);
    const summary = "Forecast and control state agree on this comfort risk.";
    await page.route("**/api/thermal/indoor-forecast?hours=24", (route) => route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
            forecast_with_plan: [{ hour: 1, ts: iso(1), predicted_indoor_temp: 19.2 }],
            forecast_no_heating: [],
            target_schedule: [{ hour: 1, target: 21, comfort_hour: true }],
            planned_actions: [],
            weather_forecast: [],
            price_forecast: [],
            forecast_status: "available",
            comfort_assessment: { state: "at_risk", summary },
        }),
    }));
    await page.route("**/api/control-state", (route) => route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ state: "comfort_at_risk", headline: "Comfort needs attention", detail: summary, reason_code: "comfort_at_risk", since: null, until: null, override_id: null, active_override_count: 0, primary_action: null, notices: [], resolved_at: iso(0) }),
    }));
    await page.goto("/?view=timeline");

    const riskPanel = page.locator("#dashboard-panel-timeline .comfort-risk-card");
    await expect(riskPanel).toHaveCount(1);
    await expect(riskPanel).toContainText("Comfort risk");
    await expect(riskPanel.getByText(summary, { exact: true })).toHaveCount(1);
});

test("timeline keeps active-plan actions separate from activity outcomes and supports every activity filter", async ({ page }) => {
    await timelineFixture(page);
    const activePlan = { id: 42, created_at: iso(-1), horizon_start: iso(-1), horizon_end: iso(23), optimizer_version: "rules_v7", cost_estimate_eur: 1.25, actions_count: 2 };
    let activityRequest = "";
    await page.route("**/api/dashboard", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ current_status: null, active_plan: activePlan }) }));
    await page.route("**/api/plans/42", (route) => route.fulfill({
        contentType: "application/json", body: JSON.stringify({
            ...activePlan, actions: [
                { id: 10, scheduled_ts: iso(-1), action_type: "zone_temp_boost", status: "executed", payload: {} },
                { id: 11, scheduled_ts: iso(1), action_type: "force_dhw_on", status: "pending", payload: {} },
            ]
        })
    }));
    await page.route("**/api/plan-activity?*", (route) => {
        activityRequest = route.request().url();
        return route.fulfill({
            contentType: "application/json", body: JSON.stringify([
                { id: 10, plan_id: 42, plan_created_at: iso(-2), optimizer_version: "rules_v7", scheduled_ts: iso(-1), action_type: "zone_temp_boost", status: "executed", executed_at: iso(-1), payload: {}, result: { verified: true } },
                { id: 17, plan_id: 17, plan_created_at: iso(-2), optimizer_version: "rules_v7", scheduled_ts: iso(-1), action_type: "normal_mode_on", status: "executed", executed_at: iso(-1), payload: {}, result: { verified: true } },
                { id: 18, plan_id: 42, plan_created_at: iso(-2), optimizer_version: "rules_v7", scheduled_ts: iso(-1), action_type: "zone_temp_boost", status: "cancelled", executed_at: null, payload: {}, result: { reason: "superseded" } },
                { id: 19, plan_id: 42, plan_created_at: iso(-2), optimizer_version: "rules_v7", scheduled_ts: iso(-1), action_type: "force_dhw_on", status: "cancelled", executed_at: null, payload: {}, result: { reason: "superseded" } },
                { id: 20, plan_id: 42, plan_created_at: iso(-2), optimizer_version: "rules_v7", scheduled_ts: iso(1), action_type: "force_dhw_on", status: "pending", executed_at: null, payload: {}, result: { reason: "restorative" } },
            ])
        });
    });

    await page.goto("/?view=timeline");
    await expect.poll(() => activityRequest).toContain("status=pending");
    await expect(activityRequest).toContain("status=dispatched");

    const activePlanView = page.locator("#dashboard-panel-timeline .plan-section").filter({ has: page.getByRole("heading", { name: "Active Plan" }) });
    await expect(activePlanView.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "1");
    await expect(activePlanView.getByRole("progressbar")).toHaveAttribute("aria-valuemax", "2");
    await expect(activePlanView.getByText("Normal mode on")).toHaveCount(0);

    const actionList = page.locator(".timeline-action-list");
    await expect(actionList.getByText("Historical outcome (Plan #17)")).toBeVisible();
    await expect(actionList.getByText("Plan replaced", { exact: true })).toHaveCount(1);
    await expect(actionList.locator("li")).toHaveCount(5);

    const activity = page.getByTestId("plan-activity");
    await activity.getByRole("button", { name: "Safety" }).click();
    await expect(activity.getByText("Heat hot water")).toBeVisible();
    await expect(activity.locator("li")).toHaveCount(1);
    await activity.getByRole("button", { name: "All" }).click();
    await expect(activity.locator("strong", { hasText: "Plan replaced" })).toHaveCount(1);
    await expect(activity.getByText("Plan #17", { exact: true })).toBeVisible();
});

test("timeline does not show a risk panel for automatic on-target control", async ({ page }) => {
    await timelineFixture(page);
    await page.route("**/api/thermal/indoor-forecast?hours=24", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ forecast_with_plan: [], forecast_no_heating: [], target_schedule: [], planned_actions: [], weather_forecast: [], price_forecast: [], forecast_status: "available", comfort_assessment: { state: "on_target", summary: "Forecast remains on target." } }) }));
    await page.goto("/?view=timeline");
    await expect(page.locator("#dashboard-panel-timeline .comfort-risk-card")).toHaveCount(0);
    await expect(page.locator("#dashboard-panel-timeline .comfort-assessment-card")).toContainText("Comfort outlook");
});

for (const source of ["prices", "indoor", "forecast", "activity", "settings"] as const) {
    test(`timeline isolates a failed ${source} source`, async ({ page }) => {
        await timelineFixture(page);
        const paths = {
            prices: "**/api/prices?hours=48",
            indoor: "**/api/indoor-temp?hours=24",
            forecast: "**/api/thermal/indoor-forecast?hours=24",
            activity: "**/api/plan-activity?*",
            settings: "**/api/settings",
        } as const;
        await page.route(paths[source], (route) => route.fulfill({ status: 500, contentType: "application/json", body: "{}" }));
        await page.goto("/?view=timeline");
        await expect(page.getByText(new RegExp(`Unavailable:.*${source} unavailable`))).toBeVisible();
        await expect(page.getByTestId("explanation-timeline")).toBeVisible();
    });
}

test("timeline does not fabricate values when every source is empty", async ({ page }) => {
    await timelineFixture(page);
    await page.route("**/api/prices?hours=48", (route) => route.fulfill({ contentType: "application/json", body: "[]" }));
    await page.route("**/api/indoor-temp?hours=24", (route) => route.fulfill({ contentType: "application/json", body: "[]" }));
    await page.route("**/api/thermal/indoor-forecast?hours=24", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ forecast_with_plan: [], forecast_no_heating: [], target_schedule: [] }) }));
    await page.route("**/api/plan-activity?*", (route) => route.fulfill({ contentType: "application/json", body: "[]" }));
    await page.route("**/api/settings", (route) => route.fulfill({ contentType: "application/json", body: "{}" }));
    await page.goto("/?view=timeline");
    await expect(page.getByTestId("explanation-timeline")).toBeVisible();
    await expect(page.locator(".timeline-price-summary")).toHaveCount(0);
    await expect(page.locator(".timeline-actual, .timeline-forecast, .timeline-comfort-band, .timeline-target")).toHaveCount(0);
    await expect(page.getByText("No planned actions or recorded outcomes in this window.")).toBeVisible();
});

for (const width of [375, 640, 768, 1280]) {
    test(`timeline remains usable at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await timelineFixture(page);
        await page.goto("/?view=timeline");
        await expect(page.getByTestId("explanation-timeline")).toBeVisible();
        await expect(page.locator(".timeline-scroll")).toBeVisible();
    });
}

test("Timeline requests each source once per shared refresh tick", async ({ page }) => {
    await page.addInitScript(() => {
        const intervals = new Map<number, () => void>();
        const originalSetInterval = window.setInterval;
        window.setInterval = ((callback: TimerHandler, timeout?: number, ...args: unknown[]) => {
            const id = originalSetInterval(callback, timeout, ...args);
            if (timeout === 30_000 && typeof callback === "function") intervals.set(id, callback as () => void);
            return id;
        }) as typeof window.setInterval;
        const originalClearInterval = window.clearInterval;
        window.clearInterval = ((id?: number) => {
            if (id !== undefined) intervals.delete(id);
            originalClearInterval(id);
        }) as typeof window.clearInterval;
        Object.defineProperty(window, "__timelineRefreshIntervals", { value: intervals });
    });
    const sourceCounts = { prices: 0, indoor: 0, forecast: 0, activity: 0, settings: 0, plan: 0 };
    const activePlan = {
        id: 4,
        created_at: iso(-1),
        horizon_start: iso(-1),
        horizon_end: iso(23),
        optimizer_version: "rules_v7",
        cost_estimate_eur: 1.25,
        actions_count: 0,
    };
    await page.route("**/api/dashboard", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ current_status: null, active_plan: activePlan }) }));
    await page.route("**/api/prices?hours=48", (route) => { sourceCounts.prices += 1; return route.fulfill({ contentType: "application/json", body: "[]" }); });
    await page.route("**/api/indoor-temp?hours=24", (route) => { sourceCounts.indoor += 1; return route.fulfill({ contentType: "application/json", body: "[]" }); });
    await page.route("**/api/thermal/indoor-forecast?hours=24", (route) => { sourceCounts.forecast += 1; return route.fulfill({ contentType: "application/json", body: JSON.stringify({ forecast_with_plan: [], forecast_no_heating: [], target_schedule: [], planned_actions: [], weather_forecast: [], price_forecast: [] }) }); });
    await page.route("**/api/plan-activity?*", (route) => { sourceCounts.activity += 1; return route.fulfill({ contentType: "application/json", body: "[]" }); });
    await page.route("**/api/settings", (route) => { sourceCounts.settings += 1; return route.fulfill({ contentType: "application/json", body: "{}" }); });
    await page.route("**/api/plans/4", (route) => { sourceCounts.plan += 1; return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...activePlan, actions: [] }) }); });

    await page.goto("/");
    await expect.poll(() => sourceCounts.forecast).toBeGreaterThan(0);
    await expect.poll(() => sourceCounts.plan).toBeGreaterThan(0);
    await expect(page.locator(".decision-summary")).toBeVisible();
    await expect(page.getByText("Loading forecast details...", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Checking plan...", { exact: true })).toHaveCount(0);
    await expect(page.locator(".decision-summary")).toContainText("No pending command");
    const beforeActivation = { ...sourceCounts };
    await page.getByRole("tab", { name: "Timeline" }).click();
    await expect(page.getByTestId("explanation-timeline")).toBeVisible();
    expect(await page.evaluate(() => (window as unknown as { __timelineRefreshIntervals: Map<number, () => void> }).__timelineRefreshIntervals.size)).toBe(1);

    expect(sourceCounts.prices).toBeGreaterThan(beforeActivation.prices);
    expect(sourceCounts.indoor).toBeGreaterThan(beforeActivation.indoor);
    expect(sourceCounts.forecast).toBeGreaterThan(beforeActivation.forecast);
    expect(sourceCounts.activity).toBeGreaterThan(beforeActivation.activity);
    expect(sourceCounts.settings).toBeGreaterThan(beforeActivation.settings);
    expect(sourceCounts.plan).toBeGreaterThan(beforeActivation.plan);
    const afterActivation = { ...sourceCounts };
    await page.evaluate(() => Array.from(
        (window as unknown as { __timelineRefreshIntervals: Map<number, () => void> }).__timelineRefreshIntervals.values(),
    ).forEach((callback) => callback()));
    await expect.poll(() => sourceCounts).toEqual({
        prices: afterActivation.prices + 1,
        indoor: afterActivation.indoor + 1,
        forecast: afterActivation.forecast + 1,
        activity: afterActivation.activity + 1,
        settings: afterActivation.settings + 1,
        plan: afterActivation.plan + 1,
    });
});

test("a delayed response for an old active plan cannot replace newer Timeline actions", async ({ page }) => {
    await page.addInitScript(() => {
        const intervals = new Map<number, () => void>();
        const originalSetInterval = window.setInterval;
        window.setInterval = ((callback: TimerHandler, timeout?: number, ...args: unknown[]) => {
            const id = originalSetInterval(callback, timeout, ...args);
            if (timeout === 30_000 && typeof callback === "function") intervals.set(id, callback as () => void);
            return id;
        }) as typeof window.setInterval;
        const originalClearInterval = window.clearInterval;
        window.clearInterval = ((id?: number) => {
            if (id !== undefined) intervals.delete(id);
            originalClearInterval(id);
        }) as typeof window.clearInterval;
        Object.defineProperty(window, "__timelineRefreshIntervals", { value: intervals });
    });
    const oldPlan = { id: 4, created_at: iso(-1), horizon_start: iso(-1), horizon_end: iso(23), optimizer_version: "rules_v7", cost_estimate_eur: 1.25, actions_count: 1 };
    const newPlan = { ...oldPlan, id: 5 };
    let useNewPlan = false;
    let releaseOldPlan!: () => void;
    const oldPlanResponse = new Promise<void>((resolve) => { releaseOldPlan = resolve; });
    await page.route("**/api/dashboard", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ current_status: null, active_plan: useNewPlan ? newPlan : oldPlan }) }));
    await page.route("**/api/plans/4", async (route) => {
        await oldPlanResponse;
        await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...oldPlan, actions: [{ id: 4, scheduled_ts: iso(1), action_type: "force_dhw_on", payload: {}, status: "pending" }] }) }).catch(() => undefined);
    });
    await page.route("**/api/plans/5", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...newPlan, actions: [{ id: 5, scheduled_ts: iso(1), action_type: "zone_temp_boost", payload: {}, status: "pending" }] }) }));

    await page.goto("/?view=timeline");
    useNewPlan = true;
    await page.evaluate(() => Array.from(
        (window as unknown as { __timelineRefreshIntervals: Map<number, () => void> }).__timelineRefreshIntervals.values(),
    ).forEach((callback) => callback()));
    const planActions = page.locator("#dashboard-panel-timeline .plan-actions");
    await expect(page.locator("#dashboard-panel-timeline .plan-next-callout").getByRole("img", { name: "Boost heating" })).toBeVisible();
    releaseOldPlan();
    await expect(planActions.getByRole("img", { name: "Boost heating" })).toBeVisible();
    await expect(planActions.getByRole("img", { name: "Heat hot water" })).toHaveCount(0);
});

for (const variant of [
    { name: "EUR with a 24-hour clock", currency: { code: "EUR", prefix: "EUR ", suffix: "", multiplier: 100, price_label: "EURc/kWh" }, hour12: false, label: "EURc/kWh", price: "EUR 42.0" },
    { name: "SEK with a 12-hour clock", currency: { code: "SEK", prefix: "", suffix: " kr", multiplier: 1, price_label: "SEK/kWh" }, hour12: true, label: "SEK/kWh", price: "0.42 kr" },
]) {
    test(`timeline uses ${variant.name} from server preferences`, async ({ page }) => {
        await timelineFixture(page);
        await page.route("**/api/currency", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(variant.currency) }));
        await page.route("**/api/time-format", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ hour12: variant.hour12 }) }));
        await page.goto("/?view=timeline");
        await expect(page.getByText(`Price (${variant.label})`)).toBeVisible();
        await expect(page.getByText(new RegExp(`Current price series: ${variant.price.replace(".", "\\.")}`))).toBeVisible();
        const axis = page.locator(".timeline-axis");
        await expect(axis).toContainText(variant.hour12 ? /AM|PM/ : /\d{2}:\d{2}/);
    });
}