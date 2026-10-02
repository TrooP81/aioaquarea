import { expect, test } from "./fixtures";

const activity = (id: number, status: string) => ({
    id,
    plan_id: 7,
    plan_created_at: "2026-10-01T10:00:00Z",
    optimizer_version: "rules_v3",
    scheduled_ts: "2026-10-01T10:00:00Z",
    action_type: "zone_temp_restore",
    status,
    executed_at: null,
    lateness_seconds: null,
    payload: {},
    result: null,
});

for (const { name, path, expectedPath } of [
    { name: "overview", path: "/?view=overview&extra=first&extra=second#preserved", expectedPath: "/?view=home&extra=first&extra=second#preserved" },
    { name: "controls", path: "/?view=controls&extra=first&extra=second#preserved", expectedPath: "/?view=home&extra=first&extra=second#preserved" },
    { name: "plan", path: "/?view=plan&extra=first&extra=second#preserved", expectedPath: "/?view=timeline&extra=first&extra=second#preserved" },
    { name: "charts", path: "/?view=charts&extra=first&extra=second#preserved", expectedPath: "/?view=under-the-hood&extra=first&extra=second#preserved" },
    { name: "status", path: "/?view=status&extra=first&extra=second#preserved", expectedPath: "/?view=under-the-hood&extra=first&extra=second#preserved" },
]) {
    test(`legacy ${name} alias replaces the URL without adding history`, async ({ page }) => {
        await page.addInitScript(() => {
            const calls: string[] = [];
            const originalPushState = window.history.pushState.bind(window.history);
            const originalReplaceState = window.history.replaceState.bind(window.history);
            window.history.pushState = ((...args: Parameters<History["pushState"]>) => {
                calls.push("push");
                return originalPushState(...args);
            }) as History["pushState"];
            window.history.replaceState = ((...args: Parameters<History["replaceState"]>) => {
                calls.push("replace");
                return originalReplaceState(...args);
            }) as History["replaceState"];
            Object.defineProperty(window, "__phase3HistoryCalls", { value: calls });
        });
        await page.goto("/");
        const previousHistoryLength = await page.evaluate(() => window.history.length);
        await page.goto(path);

        await expect(page).toHaveURL(expectedPath);
        expect(await page.evaluate(() => window.history.length)).toBe(previousHistoryLength + 1);
        expect(await page.evaluate(() => (window as unknown as { __phase3HistoryCalls: string[] }).__phase3HistoryCalls)).toContain("replace");
        expect(await page.evaluate(() => (window as unknown as { __phase3HistoryCalls: string[] }).__phase3HistoryCalls)).not.toContain("push");
    });
}

for (const { name, path, expectedPath, hash } of [
    { name: "controls", path: "/?view=controls", expectedPath: "/?view=home", hash: "#controls" },
    { name: "charts", path: "/?view=charts", expectedPath: "/?view=under-the-hood", hash: "#raw-charts" },
    { name: "status", path: "/?view=status", expectedPath: "/?view=under-the-hood", hash: "#models" },
]) {
    test(`legacy ${name} alias adds its destination hash when none is supplied`, async ({ page }) => {
        await page.addInitScript(() => {
            const scrolledIds: string[] = [];
            const originalScrollIntoView = Element.prototype.scrollIntoView;
            Element.prototype.scrollIntoView = function (...args: Parameters<Element["scrollIntoView"]>) {
                if (this.id) scrolledIds.push(this.id);
                return originalScrollIntoView.apply(this, args);
            };
            Object.defineProperty(window, "__phase3ScrolledIds", { value: scrolledIds });
        });

        await page.goto(path);

        await expect(page).toHaveURL(new RegExp(`${expectedPath.replace("?", "\\?")}${hash}`));
        await expect.poll(() => page.evaluate(() =>
            (window as unknown as { __phase3ScrolledIds: string[] }).__phase3ScrolledIds,
        )).toContain(hash.slice(1));
        if (name === "charts") {
            await expect(page.getByRole("button", { name: "Hide raw weather, price and temperature history" })).toBeVisible();
        }
    });
}

const TIMELINE_ACTIVITY_STATUSES = ["executed", "executed_unverified", "failed", "expired", "skipped", "skipped_peak", "cancelled", "pending", "executing", "dispatched"];

for (const { name, path, status } of [
    { name: "failed legacy alias", path: "/?view=plan&activity=failed#plan-action-42", status: "failed" },
    { name: "failed canonical URL", path: "/?view=timeline&activity=failed#plan-action-42", status: "failed" },
    { name: "safety legacy alias", path: "/?view=plan&activity=safety#plan-action-43", status: "pending" },
    { name: "safety canonical URL", path: "/?view=timeline&activity=safety#plan-action-43", status: "pending" },
]) {
    test(`${name} waits for activity, scrolls, focuses, and clears the highlight`, async ({ page }) => {
        const id = status === "failed" ? 42 : 43;
        let requestedStatuses: string[] = [];
        await page.addInitScript(() => {
            const scrolledIds: string[] = [];
            const originalScrollIntoView = Element.prototype.scrollIntoView;
            Element.prototype.scrollIntoView = function (...args: Parameters<Element["scrollIntoView"]>) {
                if (this.id) scrolledIds.push(this.id);
                return originalScrollIntoView.apply(this, args);
            };
            Object.defineProperty(window, "__phase3ScrolledIds", { value: scrolledIds });
        });
        await page.route("**/api/plan-activity*", async (route) => {
            requestedStatuses = new URL(route.request().url()).searchParams.getAll("status");
            await new Promise((resolve) => setTimeout(resolve, 50));
            await route.fulfill({ contentType: "application/json", body: JSON.stringify([activity(id, status)]) });
        });

        await page.goto(path);

        const target = page.locator(`#plan-action-${id}`);
        await expect(target).toHaveAttribute("aria-current", "true");
        await expect(target).toBeFocused();
        await expect(target).toHaveClass(/deep-link-target/);
        expect(requestedStatuses).toEqual(TIMELINE_ACTIVITY_STATUSES);
        expect(await page.evaluate(() => (window as unknown as { __phase3ScrolledIds: string[] }).__phase3ScrolledIds)).toContain(`plan-action-${id}`);
        await page.waitForTimeout(8_100);
        await expect(target).not.toHaveClass(/deep-link-target/);
    });
}

test("Outcome requests and renders skipped peak-price actions", async ({ page }) => {
    let requestedStatuses: string[] = [];
    await page.route("**/api/plan-activity*", (route) => {
        requestedStatuses = new URL(route.request().url()).searchParams.getAll("status");
        return route.fulfill({ contentType: "application/json", body: JSON.stringify([activity(44, "skipped_peak")]) });
    });

    await page.goto("/?view=timeline");

    await expect(page.getByTestId("plan-activity").getByText("Skipped (peak price)")).toBeVisible();
    expect(requestedStatuses).toContain("skipped_peak");
});

test("Timeline reapplies its deep link after hash navigation and clears the previous target", async ({ page }) => {
    await page.route("**/api/plan-activity*", (route) =>
        route.fulfill({ contentType: "application/json", body: JSON.stringify([activity(42, "failed"), activity(43, "failed")]) }),
    );

    await page.goto("/?view=timeline&activity=failed#plan-action-42");
    const first = page.locator("#plan-action-42");
    const second = page.locator("#plan-action-43");
    await expect(first).toHaveAttribute("aria-current", "true");

    await page.evaluate(() => { window.location.hash = "plan-action-43"; });

    await expect(second).toHaveAttribute("aria-current", "true");
    await expect(second).toBeFocused();
    await expect(first).not.toHaveClass(/deep-link-target/);
    await expect(first).not.toHaveAttribute("aria-current", "true");
});

test("newer dashboard and plan-history refreshes win over delayed older responses", async ({ page }) => {
    await page.addInitScript(() => {
        const intervals = new Map<number, () => void>();
        const original = window.setInterval;
        window.setInterval = ((callback: TimerHandler, timeout?: number, ...args: unknown[]) => {
            const id = original(callback, timeout, ...args);
            if (timeout === 30_000 && typeof callback === "function") intervals.set(id, callback as () => void);
            return id;
        }) as typeof window.setInterval;
        Object.defineProperty(window, "__phase3RefreshIntervals", { value: intervals });
    });
    let releaseDashboard!: () => void;
    let releasePlans!: () => void;
    const delayedDashboard = new Promise<void>((resolve) => { releaseDashboard = resolve; });
    const delayedPlans = new Promise<void>((resolve) => { releasePlans = resolve; });
    let dashboardRaceRequests = 0;
    let planRaceRequests = 0;
    let startDashboardRace = false;
    let startPlanRace = false;
    const dashboard = (todayKwh: number) => ({
        current_status: null,
        current_status_fresh: false,
        current_status_age_seconds: null,
        current_price: null,
        today_kwh: todayKwh,
        today_cost_eur: null,
        today_cost_currency: "EUR",
        today_cost_priced_kwh: 0,
        today_cost_unpriced_kwh: 0,
        today_cost_priced_amount: 0,
        today_cost_coverage_pct: 0,
        today_cost_complete: false,
        active_plan: null,
        has_override: false,
        override_id: null,
    });
    const plan = (id: number, actions: number) => ({
        id,
        created_at: "2026-10-01T10:00:00Z",
        horizon_start: "2026-10-01T10:00:00Z",
        horizon_end: "2026-10-02T10:00:00Z",
        optimizer_version: "rules_v3",
        cost_estimate_eur: 1,
        actions_count: actions,
        status: "active",
        status_reason: null,
        superseded_by_plan_id: null,
    });
    await page.route("**/api/dashboard", async (route) => {
        if (!startDashboardRace) {
            await route.fulfill({ contentType: "application/json", body: JSON.stringify(dashboard(10)) });
            return;
        }
        dashboardRaceRequests += 1;
        if (dashboardRaceRequests === 1) {
            await delayedDashboard;
            await route.fulfill({ contentType: "application/json", body: JSON.stringify(dashboard(1)) }).catch(() => undefined);
            return;
        }
        await route.fulfill({ contentType: "application/json", body: JSON.stringify(dashboard(99)) });
    });
    await page.route("**/api/plans?limit=50", async (route) => {
        if (!startPlanRace) {
            await route.fulfill({ contentType: "application/json", body: JSON.stringify([plan(1, 1)]) });
            return;
        }
        planRaceRequests += 1;
        if (planRaceRequests === 1) {
            await delayedPlans;
            await route.fulfill({ contentType: "application/json", body: JSON.stringify([plan(1, 1)]) }).catch(() => undefined);
            return;
        }
        await route.fulfill({ contentType: "application/json", body: JSON.stringify([plan(2, 9)]) });
    });

    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.getByText("10.0 kWh", { exact: true })).toBeVisible();
    startDashboardRace = true;
    await page.evaluate(() => Array.from(
        (window as unknown as { __phase3RefreshIntervals: Map<number, () => void> }).__phase3RefreshIntervals.values(),
    ).forEach((callback) => callback()));
    await expect.poll(() => dashboardRaceRequests).toBe(1);
    await page.evaluate(() => Array.from(
        (window as unknown as { __phase3RefreshIntervals: Map<number, () => void> }).__phase3RefreshIntervals.values(),
    ).forEach((callback) => callback()));
    await expect.poll(() => dashboardRaceRequests).toBe(2);
    await expect(page.getByText("99.0 kWh", { exact: true })).toBeVisible();
    releaseDashboard();
    await expect(page.getByText("99.0 kWh", { exact: true })).toBeVisible();

    await page.getByRole("tab", { name: "Timeline" }).click();
    await expect(page.getByText("1 actions", { exact: true })).toBeVisible();
    startPlanRace = true;
    await page.evaluate(() => Array.from(
        (window as unknown as { __phase3RefreshIntervals: Map<number, () => void> }).__phase3RefreshIntervals.values(),
    ).forEach((callback) => callback()));
    await expect.poll(() => planRaceRequests).toBe(1);
    await page.evaluate(() => Array.from(
        (window as unknown as { __phase3RefreshIntervals: Map<number, () => void> }).__phase3RefreshIntervals.values(),
    ).forEach((callback) => callback()));
    await expect.poll(() => planRaceRequests).toBe(2);
    await expect(page.getByText("9 actions", { exact: true })).toBeVisible();
    releasePlans();
    await expect(page.getByText("9 actions", { exact: true })).toBeVisible();
});

test("Timeline waits for activation and refreshes once per shared clock tick", async ({ page }) => {
    await page.addInitScript(() => {
        const intervals = new Map<number, () => void>();
        const original = window.setInterval;
        window.setInterval = ((callback: TimerHandler, timeout?: number, ...args: unknown[]) => {
            const id = original(callback, timeout, ...args);
            if (timeout === 30_000 && typeof callback === "function") intervals.set(id, callback as () => void);
            return id;
        }) as typeof window.setInterval;
        const originalClear = window.clearInterval;
        window.clearInterval = ((id?: number) => {
            if (id !== undefined) intervals.delete(id);
            originalClear(id);
        }) as typeof window.clearInterval;
        Object.defineProperty(window, "__phase3RefreshIntervals", { value: intervals });
    });
    const requestCounts = { activity: 0, plans: 0 };
    await page.route("**/api/plan-activity*", (route) => {
        requestCounts.activity += 1;
        return route.fulfill({ contentType: "application/json", body: "[]" });
    });
    await page.route("**/api/plans?limit=50", (route) => {
        requestCounts.plans += 1;
        return route.fulfill({ contentType: "application/json", body: "[]" });
    });

    await page.goto("/");
    expect(requestCounts).toEqual({ activity: 0, plans: 0 });
    await page.getByRole("tab", { name: "Timeline" }).click();
    await expect(page.getByText("No completed, failed, skipped, or replaced optimizer actions yet.")).toBeVisible();
    await expect(page.getByText("No past plans yet.")).toBeVisible();
    const initialRequestCounts = { ...requestCounts };
    expect(
        await page.evaluate(() =>
            (window as unknown as { __phase3RefreshIntervals: Map<number, () => void> }).__phase3RefreshIntervals.size,
        ),
    ).toBe(1);

    for (let tick = 0; tick < 2; tick += 1) {
        await page.evaluate(() => {
            Array.from(
                (window as unknown as { __phase3RefreshIntervals: Map<number, () => void> }).__phase3RefreshIntervals.values(),
            ).forEach((callback) => callback());
        });
        await expect.poll(() => requestCounts).toEqual({
            activity: initialRequestCounts.activity + tick + 1,
            plans: initialRequestCounts.plans + tick + 1,
        });
    }
});