import { defaultBody, demandDataQuality, expect, manualCOPObservation, manualCOPResponse, retiredCopModel, test, type Page } from "./fixtures";

interface ManualLedgerState {
    rows: ReturnType<typeof manualCOPObservation>[];
    devices: string[];
    defaultTimezone: string;
    enabled: boolean;
    mutationStatus: number;
    unavailable: boolean;
    nonready: "no_data" | "insufficient_data" | "stale" | null;
    writes: Array<{ method: string; body: Record<string, unknown> }>;
    settings: unknown[];
    revision: bigint;
    getCount: number;
}

async function manualLedger(page: Page, initialRows = [] as ReturnType<typeof manualCOPObservation>[], sharedState?: ManualLedgerState) {
    const state: ManualLedgerState = sharedState ?? {
        rows: initialRows,
        devices: ["fixture-device"],
        defaultTimezone: "Europe/Stockholm",
        enabled: false,
        mutationStatus: 200,
        unavailable: false,
        nonready: null,
        writes: [] as Array<{ method: string; body: Record<string, unknown> }>,
        settings: [] as unknown[],
        revision: BigInt("9007199254740993"),
        getCount: 0,
    };
    await page.route("**/api/settings", async (route) => {
        const body = route.request().postDataJSON();
        state.settings.push(body);
        state.enabled = body.settings.manual_cop_monitoring_enabled === "true";
        return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });
    await page.route("**/api/cop/manual**", async (route) => {
        const request = route.request();
        const url = new URL(request.url());
        if (request.method() === "GET") {
            state.getCount += 1;
            const series = url.searchParams.get("granularity") === "month" ? "month" : "day";
            const device = url.searchParams.get("device_id");
            const before = url.searchParams.get("before");
            const rows = device ? state.rows
                .filter((row) => row.device_id === device && row.granularity === series && (!before || row.period_date < before))
                .sort((left, right) => right.period_date.localeCompare(left.period_date)) : [];
            const observations = rows.slice(0, 90);
            const status = !state.enabled ? "disabled" : state.unavailable ? "unavailable" : state.nonready ?? "ready";
            const ready = status === "ready";
            const reasons = {
                disabled: "forecasting_disabled",
                unavailable: "optional_ml_dependencies_unavailable",
                ready: "matched_clock_retrospective_folds",
                no_data: "no_daily_observations",
                insufficient_data: "requires_28_consecutive_closed_days",
                stale: "latest_closed_day_precedes_yesterday",
            };
            return route.fulfill({
                status: 200, contentType: "application/json", body: JSON.stringify({
                    ...manualCOPResponse, devices: state.devices, default_timezone: state.defaultTimezone, device_id: device, granularity: series, observations,
                    next_before: rows.length > 90 ? rows[89].period_date : null,
                    forecasting_enabled: state.enabled,
                    monitoring: {
                        ...manualCOPResponse.monitoring,
                        status,
                        reason: reasons[status],
                        fingerprint: `fixture-${state.getCount}`,
                        target_date: ready ? "2026-10-05" : null,
                        counts: { ...manualCOPResponse.monitoring.counts, available_closed_days: ready ? 28 : 0, fold_training_rows: ready ? [20, 21, 22, 23, 24, 25, 26] : [] },
                        forecast: ready ? { value: 4.2, lower: 3.4, upper: 5.2, log_daily_trend: .01, daily_trend_pct: 1.005, band_label: "experimental empirical log-error band, uncalibrated" } : null,
                    },
                })
            });
        }
        const body = request.postDataJSON();
        state.writes.push({ method: request.method(), body });
        if (state.mutationStatus !== 200) return route.fulfill({ status: state.mutationStatus, body: "{}" });
        const [, , , , device, series, periodDate] = url.pathname.split("/");
        const existing = state.rows.find((row) => row.device_id === decodeURIComponent(device) && row.granularity === series && row.period_date === periodDate);
        if (body.expected_revision !== (existing?.revision ?? "0")) return route.fulfill({ status: 409, body: "{}" });
        if (request.method() === "DELETE") {
            state.rows = state.rows.filter((row) => row !== existing);
            return route.fulfill({ status: 204 });
        }
        if (existing && state.revision <= BigInt(existing.revision)) state.revision = BigInt(existing.revision) + BigInt(1);
        const row = {
            ...manualCOPObservation(periodDate, body.value, String(state.revision++), series as "day" | "month"),
            device_id: decodeURIComponent(device), observed_at: body.observed_at, completeness: body.completeness, timezone: body.timezone,
        };
        state.rows = state.rows.filter((previous) => previous !== existing).concat(row);
        return route.fulfill({ status: existing ? 200 : 201, contentType: "application/json", body: JSON.stringify(row) });
    });
    return state;
}

async function openManual(page: Page) {
    await page.goto("/?view=under-the-hood");
    await page.getByRole("button", { name: "Show diagnostics" }).click();
    const panel = page.locator('section[aria-labelledby="manual-cop-heading"]');
    await panel.getByRole("combobox", { name: "Device", exact: true }).selectOption("fixture-device");
    await expect(panel.getByRole("button", { name: "Refresh readings" })).toBeEnabled();
    return panel;
}

test.describe("Manual controller ledger", () => {
    test("keeps a confirmed draft timezone and period when the setting default changes", async ({ page }) => {
        const state = await manualLedger(page);
        const panel = await openManual(page);
        await panel.getByLabel("Period date").fill("2026-09-30");
        await panel.getByLabel("Controller timezone confirmed").check();
        state.defaultTimezone = "Asia/Tokyo";
        await panel.getByRole("button", { name: "Refresh readings" }).click();
        await expect(panel.getByRole("button", { name: "Refresh readings" })).toBeEnabled();
        await expect(panel.getByRole("textbox", { name: "Controller timezone", exact: true })).toHaveValue("Europe/Stockholm");
        await expect(panel.getByLabel("Controller timezone confirmed")).toBeChecked();
        await expect(panel.getByLabel("Period date")).toHaveValue("2026-09-30");
    });

    test("keeps 90-plus-one cursors independent by device and day/month series", async ({ page }) => {
        const rows = [] as ReturnType<typeof manualCOPObservation>[];
        for (let index = 0; index < 91; index += 1) {
            const day = new Date(Date.UTC(2026, 9, 4 - index)).toISOString().slice(0, 10);
            const month = new Date(Date.UTC(2026, 9 - index, 1)).toISOString().slice(0, 10);
            rows.push(manualCOPObservation(day, 9.7, String(index + 1)));
            rows.push(manualCOPObservation(month, 3.9, String(index + 92), "month"));
        }
        rows.push({ ...manualCOPObservation("2026-10-04", 3.7, "200"), device_id: "second-device" });
        const state = await manualLedger(page, rows);
        state.devices.push("second-device");
        const panel = await openManual(page);
        await expect(panel.locator(".manual-reading")).toHaveCount(90);
        await panel.getByRole("button", { name: "Older readings" }).click();
        await expect(panel.locator(".manual-reading")).toHaveCount(1);
        await panel.getByRole("button", { name: "Month", exact: true }).click();
        await expect(panel.locator(".manual-reading")).toHaveCount(90);
        await panel.getByRole("button", { name: "Older readings" }).click();
        await expect(panel.locator(".manual-reading")).toHaveCount(1);
        await panel.getByRole("button", { name: "Day", exact: true }).click();
        await expect(panel.locator(".manual-reading")).toHaveCount(1);
        await panel.getByRole("combobox", { name: "Device", exact: true }).selectOption("second-device");
        await expect(panel.locator(".manual-value")).toHaveText("3.7");
        await expect(panel.getByRole("button", { name: "Newest readings" })).toBeDisabled();
        await panel.getByRole("combobox", { name: "Device", exact: true }).selectOption("fixture-device");
        await expect(panel.locator(".manual-reading")).toHaveCount(1);
        await expect(panel.getByRole("button", { name: "Newest readings" })).toBeEnabled();
        await panel.getByRole("button", { name: "Newest readings" }).click();
        await expect(panel.locator(".manual-reading")).toHaveCount(90);
        await panel.getByRole("button", { name: "Month", exact: true }).click();
        await expect(panel.locator(".manual-reading")).toHaveCount(1);
    });

    test("enters 9.7 while forecasting is off, refuses duplicates and corrects opaque tokens", async ({ page }) => {
        const state = await manualLedger(page);
        const panel = await openManual(page);
        await panel.getByLabel("Period date").fill("2026-10-04");
        await panel.getByLabel("COP value").fill("9.7");
        await panel.getByLabel("Observed at (UTC)").fill("2026-10-04T08:00");
        await panel.getByLabel("Controller timezone confirmed").check();
        await panel.getByRole("button", { name: "Save reading" }).click();
        await expect(panel.locator(".manual-value")).toHaveText("9.7");
        expect(state.writes[0].body.expected_revision).toBe("0");
        await expect(panel).toContainText("Approximate");
        await expect(panel).toContainText("Measured: no");
        await expect(panel.getByTestId("manual-projection")).toHaveCount(0);

        await panel.getByLabel("Period date").fill("2026-10-04");
        await panel.getByLabel("COP value").fill("9.7");
        await panel.getByLabel("Controller timezone confirmed").check();
        await panel.getByRole("button", { name: "Save reading" }).click();
        await expect(panel.getByRole("status")).toContainText("Current readings refreshed");
        await expect(panel.locator(".manual-reading")).toHaveCount(1);

        await panel.getByRole("button", { name: "Edit reading 2026-10-04" }).click();
        await expect(panel.getByLabel("Period date")).toBeDisabled();
        await panel.getByLabel("COP value").fill("9.8");
        await panel.getByRole("button", { name: "Save correction" }).click();
        await expect(panel.locator(".manual-value")).toHaveText("9.8");
        expect(state.writes[2].body.expected_revision).toBe("9007199254740993");
        page.once("dialog", (dialog) => dialog.accept());
        await panel.getByRole("button", { name: "Delete reading 2026-10-04" }).click();
        await expect(panel.locator(".manual-reading")).toHaveCount(0);
        expect(state.writes[3].body.expected_revision).toBe("9007199254740994");
        expect(state.writes.every((write) => typeof write.body.expected_revision === "string")).toBe(true);
        expect(state.settings).toEqual([]);
    });

    test("keeps yesterday, repeated selected day and October month distinct", async ({ page }) => {
        await manualLedger(page, [
            manualCOPObservation("2026-10-04", 9.7), manualCOPObservation("2026-10-03", 3.7, "2"),
            manualCOPObservation("2026-10-01", 3.9, "3", "month"),
        ]);
        const panel = await openManual(page);
        await expect(panel.locator(".manual-value")).toHaveText(["9.7", "3.7"]);
        await panel.getByRole("button", { name: "Month", exact: true }).click();
        await expect(panel.locator(".manual-value")).toHaveText(["3.9"]);
        await expect(panel.getByLabel("Period month")).toHaveAttribute("type", "month");
        await panel.getByRole("button", { name: "Day", exact: true }).click();
        await expect(panel.locator(".manual-value")).toHaveText(["9.7", "3.7"]);
        await expect(panel.locator(".manual-reading")).toHaveCount(2);
    });

    test("refreshes conflicts and clears forecasts on failures, missing libraries and disablement", async ({ page }) => {
        const state = await manualLedger(page, [manualCOPObservation()]);
        const panel = await openManual(page);
        await panel.getByLabel("Experimental forecasting").check();
        await expect(panel.getByTestId("manual-projection")).toHaveText("4.20");
        expect(state.settings[0]).toEqual({ settings: { manual_cop_monitoring_enabled: "true" } });
        await panel.getByRole("button", { name: "Edit reading 2026-10-04" }).click();
        state.rows = [manualCOPObservation("2026-10-04", 5.5, "9007199254740995")];
        await panel.getByLabel("COP value").fill("8.1");
        await panel.getByRole("button", { name: "Save correction" }).click();
        await expect(panel.getByRole("status")).toContainText("Current readings refreshed");
        await expect(panel.locator(".manual-value")).toHaveText("5.5");
        state.mutationStatus = 503;
        page.once("dialog", (dialog) => dialog.accept());
        await panel.getByRole("button", { name: "Delete reading 2026-10-04" }).click();
        await expect(panel.getByRole("alert")).toContainText("503");
        await expect(panel.getByTestId("manual-projection")).toHaveCount(0);
        await expect(page.getByRole("heading", { name: "Why it decides" })).toBeVisible();
        state.unavailable = true;
        await panel.getByRole("button", { name: "Refresh readings" }).click();
        await expect(panel.getByRole("heading", { name: "Forecast unavailable", exact: true })).toBeVisible();
        await expect(panel.getByTestId("manual-projection")).toHaveCount(0);
        await panel.getByLabel("Experimental forecasting").uncheck();
        await expect(panel.getByRole("heading", { name: "Forecasting off" })).toBeVisible();
        await expect(panel.getByRole("button", { name: "Save reading" })).toBeEnabled();
        expect(state.settings[1]).toEqual({ settings: { manual_cop_monitoring_enabled: "false" } });
    });

    test("holds forecast invalidation until the post-mutation snapshot arrives", async ({ page }) => {
        const state = await manualLedger(page, [manualCOPObservation()]);
        state.enabled = true;
        const panel = await openManual(page);
        await expect(panel.getByTestId("manual-projection")).toHaveText("4.20");
        let release: (() => void) | undefined;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        await page.route("**/api/cop/manual?**", async (route) => {
            await gate;
            return route.fallback();
        });
        try {
            await panel.getByRole("button", { name: "Edit reading 2026-10-04" }).click();
            await panel.getByLabel("COP value").fill("8.1");
            const snapshot = page.waitForRequest((request) => request.method() === "GET" && request.url().includes("/api/cop/manual"));
            await panel.getByRole("button", { name: "Save correction" }).click();
            await snapshot;
            await expect(panel.getByTestId("manual-projection")).toHaveCount(0);
            release?.();
            await expect(panel.getByTestId("manual-projection")).toHaveText("4.20");
            await expect(panel.locator(".manual-value")).toHaveText("8.1");
        } finally {
            release?.();
        }
    });

    test("discards an obsolete daily response after selecting the monthly series", async ({ page }) => {
        await manualLedger(page, [manualCOPObservation(), manualCOPObservation("2026-10-01", 3.9, "3", "month")]);
        const panel = await openManual(page);
        let release: (() => void) | undefined;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        let captured = false;
        await page.route("**/api/cop/manual?**", async (route) => {
            if (new URL(route.request().url()).searchParams.get("granularity") !== "day" || captured) return route.fallback();
            captured = true;
            await gate;
            return route.fallback();
        });
        try {
            await panel.getByRole("button", { name: "Refresh readings" }).click();
            await expect.poll(() => captured).toBe(true);
            await panel.getByRole("button", { name: "Month", exact: true }).click();
            await expect(panel.locator(".manual-value")).toHaveText("3.9");
            release?.();
            await expect(panel.getByRole("button", { name: "Month", exact: true })).toHaveAttribute("aria-pressed", "true");
            await expect(panel.locator(".manual-value")).toHaveText("3.9");
            await expect(panel.getByTestId("manual-projection")).toHaveCount(0);
        } finally {
            release?.();
        }
    });

    for (const [status, heading] of [
        ["no_data", "No daily readings"],
        ["insufficient_data", "Insufficient daily evidence"],
        ["stale", "Daily evidence is stale"],
    ] as const) {
        test(`clears ready values in ${status} state after a loading snapshot`, async ({ page }) => {
            const state = await manualLedger(page, [manualCOPObservation()]);
            state.enabled = true;
            const panel = await openManual(page);
            await expect(panel.getByTestId("manual-projection")).toHaveText("4.20");
            state.nonready = status;
            if (status === "no_data") state.rows = [];
            let release: (() => void) | undefined;
            const gate = new Promise<void>((resolve) => { release = resolve; });
            await page.route("**/api/cop/manual?**", async (route) => {
                await gate;
                return route.fallback();
            });
            try {
                const snapshot = page.waitForRequest((request) => request.method() === "GET" && request.url().includes("/api/cop/manual"));
                await panel.getByRole("button", { name: "Refresh readings" }).click();
                await snapshot;
                await expect(panel).toHaveAttribute("aria-busy", "true");
                await expect(panel.getByTestId("manual-projection")).toHaveCount(0);
                release?.();
                await expect(panel.getByRole("heading", { name: heading, exact: true })).toBeVisible();
                await expect(panel.getByTestId("manual-projection")).toHaveCount(0);
                await expect(panel.getByRole("button", { name: "Save reading" })).toBeEnabled();
                if (status === "no_data") {
                    await expect(panel.locator(".manual-reading")).toHaveCount(0);
                    await expect(panel.getByText("No readings.", { exact: true })).toBeVisible();
                }
            } finally {
                release?.();
            }
        });
    }

    test("refuses stale corrections from an independent browser tab", async ({ page }) => {
        const state = await manualLedger(page, [manualCOPObservation()]);
        const primary = await openManual(page);
        const partner = await page.context().newPage();
        try {
            await partner.route("**/api/**", (route) => {
                const request = route.request();
                return route.fulfill({
                    status: 200,
                    contentType: "application/json",
                    body: JSON.stringify(defaultBody(new URL(request.url()).pathname, request.method())),
                });
            });
            await manualLedger(partner, [], state);
            const secondary = await openManual(partner);
            await primary.getByRole("button", { name: "Edit reading 2026-10-04" }).click();
            await secondary.getByRole("button", { name: "Edit reading 2026-10-04" }).click();
            await secondary.getByLabel("COP value").fill("9.8");
            await secondary.getByRole("button", { name: "Save correction" }).click();
            await expect(secondary.locator(".manual-value")).toHaveText("9.8");
            await primary.getByLabel("COP value").fill("9.6");
            await primary.getByRole("button", { name: "Save correction" }).click();
            await expect(primary.getByRole("status")).toContainText("Current readings refreshed");
            await expect(primary.locator(".manual-value")).toHaveText("9.8");
            expect(state.writes.map((write) => write.body.expected_revision)).toEqual([
                "9007199254740993", "9007199254740993",
            ]);
            expect(state.rows[0].revision).toBe("9007199254740994");
            expect(state.settings).toEqual([]);
            await expect(primary.getByTestId("manual-projection")).toHaveCount(0);
        } finally {
            await partner.close();
        }
    });

    test("discards an obsolete failed daily response after a newer monthly generation", async ({ page }) => {
        await page.addInitScript(() => {
            const forward = window.fetch.bind(window);
            window.fetch = (resource, options) => forward(resource, { ...options, signal: undefined });
        });
        await manualLedger(page, [manualCOPObservation(), manualCOPObservation("2026-10-01", 3.9, "3", "month")]);
        const panel = await openManual(page);
        let release: (() => void) | undefined;
        let started: (() => void) | undefined;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        const captured = new Promise<void>((resolve) => { started = resolve; });
        let blocked = false;
        await page.route("**/api/cop/manual?**", async (route) => {
            if (new URL(route.request().url()).searchParams.get("granularity") !== "day" || blocked) return route.fallback();
            blocked = true;
            started?.();
            await gate;
            return route.fulfill({ status: 503, contentType: "application/json", body: "{}" });
        });
        try {
            await panel.getByRole("button", { name: "Refresh readings" }).click();
            await captured;
            await panel.getByRole("button", { name: "Month", exact: true }).click();
            await expect(panel.locator(".manual-value")).toHaveText("3.9");
            const obsolete = page.waitForResponse((response) => response.status() === 503 && response.url().includes("/api/cop/manual"));
            release?.();
            await (await obsolete).finished();
            await expect(panel.getByRole("button", { name: "Month", exact: true })).toHaveAttribute("aria-pressed", "true");
            await expect(panel.locator(".manual-value")).toHaveText("3.9");
            await expect(panel.getByRole("alert")).toHaveCount(0);
            await expect(panel.getByTestId("manual-projection")).toHaveCount(0);
        } finally {
            release?.();
        }
    });

    test("holds forecast invalidation through deletion until the nonready snapshot arrives", async ({ page }) => {
        const state = await manualLedger(page, [manualCOPObservation()]);
        state.enabled = true;
        const panel = await openManual(page);
        await expect(panel.getByTestId("manual-projection")).toHaveText("4.20");
        state.nonready = "no_data";
        let release: (() => void) | undefined;
        const gate = new Promise<void>((resolve) => { release = resolve; });
        await page.route("**/api/cop/manual?**", async (route) => {
            await gate;
            return route.fallback();
        });
        try {
            page.once("dialog", (dialog) => dialog.accept());
            const snapshot = page.waitForRequest((request) => request.method() === "GET" && request.url().includes("/api/cop/manual"));
            await panel.getByRole("button", { name: "Delete reading 2026-10-04" }).click();
            await snapshot;
            await expect(panel.getByTestId("manual-projection")).toHaveCount(0);
            await expect(panel).toHaveAttribute("aria-busy", "true");
            release?.();
            await expect(panel.getByRole("heading", { name: "No daily readings", exact: true })).toBeVisible();
            await expect(panel.locator(".manual-reading")).toHaveCount(0);
            await expect(panel.getByTestId("manual-projection")).toHaveCount(0);
            expect(state.writes).toEqual([
                { method: "DELETE", body: { expected_revision: "9007199254740993" } },
            ]);
        } finally {
            release?.();
        }
    });
});

const statusWithFreshData = {
    configured_layer: "rules_only",
    active_layer: "rules_v7",
    fallback_layer: "rules_v7",
    last_plan: { version: "rules_v7", engine: "rules", fell_back: false, created_at: null },
    data_freshness: {
        latest_device_status: "2026-09-30T10:00:00Z",
        age_seconds: 0,
        stale_after_seconds: 300,
        fresh: true,
    },
    planning_data_quality: { control_allowed: true, reasons: [] },
    cop_model: retiredCopModel,
    demand_model: { trained: false, last_trained: null as string | null, samples: 8, source_records: 42, metrics: { mae: 0.456, samples: 8 }, unavailable_reason: null as string | null, data_quality: { ...demandDataQuality, usable_samples: 168, minimum_samples: 168, remaining_samples: 0, ready_to_train: true } },
    thermal_model: {
        calibrated: false,
        tank_heating_rate: 0,
        confidence: "default",
        indoor_heating_confidence: "default",
        indoor_heating_samples: 0,
        last_calibrated: null,
    },
};

const controlStates = {
    paused_by_user: {
        state: "paused_by_user",
        headline: "Paused by operator",
        detail: "Operator control is holding the pump off.",
    },
    observing: {
        state: "observing",
        headline: "Learning in observation",
        detail: "Commands remain paused while evidence is collected.",
    },
    holding: {
        state: "holding",
        headline: "Holding for comfort",
        detail: "The optimizer is holding the current state.",
    },
    automatic: {
        state: "automatic",
        headline: "Automatic schedule active",
        detail: "The optimizer may dispatch the next safe action.",
    },
} as const;

function controlStateResponse(state: keyof typeof controlStates) {
    return {
        ...controlStates[state],
        reason_code: state,
        since: null,
        until: null,
        override_id: null,
        active_override_count: 0,
        primary_action: null,
        notices: [],
        resolved_at: "2026-01-01T00:00:00+00:00",
    };
}

async function mockOptimizerStatus(page: import("@playwright/test").Page, status: unknown = statusWithFreshData) {
    await page.route("**/api/optimizer/status", (route) =>
        route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(status) }),
    );
}

test.describe("Models status split", () => {
    test("keeps diagnostics collapsed with an accessible keyboard disclosure", async ({ page }) => {
        await page.goto("/?view=under-the-hood");
        const toggle = page.getByRole("button", { name: "Show diagnostics" });
        await expect(page.getByRole("heading", { name: "Why it decides" })).toBeVisible();
        await expect(toggle).toHaveAttribute("aria-expanded", "false");
        await expect(toggle).toHaveAttribute("aria-controls", "optimizer-diagnostics-panel");
        await expect(page.locator("#optimizer-diagnostics-panel")).toHaveCount(0);
        for (const model of ["Demand Model", "Thermal Model", "Comfort Model"]) {
            await expect(page.locator('section[aria-labelledby="why-it-decides-heading"]')).toContainText(model);
            await expect(page.locator('section[aria-labelledby="why-it-decides-heading"]').getByRole("heading", { name: model })).toHaveCount(0);
        }
        await toggle.focus();
        await page.keyboard.press("Enter");
        const expandedToggle = page.getByRole("button", { name: "Hide diagnostics" });
        await expect(expandedToggle).toHaveAttribute("aria-expanded", "true");
        await expect(page.locator("#optimizer-diagnostics-panel")).toBeVisible();
        await expandedToggle.focus();
        await page.keyboard.press("Space");
        const collapsedToggle = page.getByRole("button", { name: "Show diagnostics" });
        await expect(collapsedToggle).toHaveAttribute("aria-expanded", "false");
        await collapsedToggle.focus();
        await page.keyboard.press("Space");
        await expect(page.getByRole("button", { name: "Hide diagnostics" })).toHaveAttribute("aria-expanded", "true");
    });

    test("keeps Models text at the scoped 0.8rem readability floor", async ({ page }) => {
        await page.goto("/?view=under-the-hood");
        await page.getByRole("button", { name: "Show diagnostics" }).click();

        const undersizedText = await page.locator("#dashboard-panel-under-the-hood").locator("*").evaluateAll((elements) =>
            elements
                .filter((element) => Array.from(element.childNodes).some(
                    (node) => node.nodeType === Node.TEXT_NODE && Boolean(node.textContent?.trim()),
                ))
                .filter((element) => parseFloat(getComputedStyle(element).fontSize) < 12.8)
                .map((element) => ({ tag: element.tagName, text: element.textContent?.trim() })),
        );

        expect(undersizedText).toEqual([]);
    });

    test("keeps the summary when an optional diagnostics request aborts", async ({ page }) => {
        await page.route("**/api/thermal/forecast-scorecard", (route) => route.abort());
        await page.goto("/?view=under-the-hood");
        await expect(page.getByText(/The active engine is/)).toBeVisible();
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        await expect(page.getByRole("heading", { name: "Forecast validation" })).toBeVisible();
        await expect(page.getByRole("heading", { name: "Forecast validation" }).locator("..")).toContainText("Unavailable.");
    });

    test("isolates all optional diagnostic failures and keeps the decision summary", async ({ page }) => {
        for (const path of [
            "/api/comfort-model/status",
            "/api/indoor-temp/latest",
            "/api/thermal/forecast-scorecard",
            "/api/sensors/diagnostics",
        ]) {
            await page.route(`**${path}`, (route) => route.abort());
        }

        await page.goto("/?view=under-the-hood");
        await expect(page.getByRole("heading", { name: "Why it decides" })).toBeVisible();
        await expect(page.getByText(/The active engine is Rules\./)).toBeVisible();
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        await expect(page.getByRole("heading", { name: "Forecast validation" }).locator("..")).toContainText("Unavailable.");
        await expect(page.getByRole("heading", { name: "Sensor diagnostics" }).locator("..")).toContainText("Unavailable.");
        await expect(page.getByRole("heading", { name: "SmartThings indoor temperature" }).locator("..")).toContainText("Unavailable.");
        await expect(page.getByText("Failed to load optimizer status")).toHaveCount(0);
    });

    test("uses the control-state wording without contradictory summary text", async ({ page }) => {
        await mockOptimizerStatus(page);
        await page.unroute("**/api/control-state");
        await page.route("**/api/control-state", (route) => {
            const state = new URL(page.url()).searchParams.get("state") as keyof typeof controlStates;
            return route.fulfill({
                status: 200,
                contentType: "application/json",
                body: JSON.stringify(controlStateResponse(controlStates[state] ? state : "automatic")),
            });
        });

        for (const state of Object.keys(controlStates) as Array<keyof typeof controlStates>) {
            await page.goto(`/?view=under-the-hood&state=${state}`);
            const summary = page.locator("#dashboard-panel-under-the-hood");
            await expect(summary.getByRole("heading", { name: "Why it decides" })).toBeVisible();
            const controlSummary = summary.locator("p").filter({ hasText: controlStates[state].headline });
            await expect(controlSummary).toBeVisible();
            await expect(controlSummary).toContainText(controlStates[state].detail);
            await expect(summary.getByText(/automatic commands are paused until fresh data returns/i)).toHaveCount(0);
            const plainLanguage = await summary.locator('section[aria-labelledby="why-it-decides-heading"] p').evaluateAll(
                (paragraphs, headline) => paragraphs.filter((paragraph) => !paragraph.textContent?.includes(headline as string)).map((paragraph) => paragraph.textContent ?? "").join(" "),
                controlStates[state].headline,
            );
            const contradictoryText = state === "automatic"
                ? /paused by operator|commands remain paused|holding the pump off/i
                : /automatic schedule active|may dispatch the next safe action/i;
            expect(plainLanguage).not.toMatch(contradictoryText);
        }
    });

    test("retains the pre-split Models fields and sections when diagnostics are expanded", async ({ page }) => {
        await mockOptimizerStatus(page);
        await page.goto("/?view=under-the-hood");
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        const diagnostics = page.locator("#optimizer-diagnostics-panel");

        for (const heading of [
            "Decision engine details",
            "Seasonal calibration",
            "Sensor diagnostics",
            "Space-heating gate",
            "COP retired",
            "Models",
            "Forecast validation",
            "Training",
            "SmartThings indoor temperature",
        ]) {
            await expect(page.getByRole("heading", { name: heading })).toBeVisible();
        }
        for (const field of [
            "Demand Model",
            "Thermal Model",
            "Decision engine",
        ]) {
            await expect(diagnostics.getByText(field, { exact: true })).toBeVisible();
        }
        await expect(page.getByRole("heading", { name: "Space-heating gate" })).toBeVisible();
        for (const button of ["Train Demand", "Train Comfort Model", "Calibrate Thermal"]) {
            await expect(page.getByRole("button", { name: button })).toBeVisible();
        }
        const diagnosticsSectionsAreSiblings = await diagnostics.locator("h3").evaluateAll(
            (headings, panel) => headings.every((heading) => heading.closest("section")?.parentElement === panel),
            await diagnostics.elementHandle(),
        );
        expect(diagnosticsSectionsAreSiblings).toBe(true);
    });

    test("ignores legacy COP readiness and retains Demand artifact evidence", async ({ page }) => {
        await mockOptimizerStatus(page, {
            ...statusWithFreshData,
            cop_model: { ...statusWithFreshData.cop_model, trained: true, last_trained: "2026-10-01T12:00:00+00:00", metrics: { mae: 0.123, samples: 50 } },
            demand_model: { ...statusWithFreshData.demand_model, trained: true, last_trained: "2026-10-01T12:00:00+00:00" },
        });
        await page.goto("/?view=under-the-hood");
        await page.getByRole("button", { name: "Show diagnostics" }).click();

        await expect(page.getByRole("heading", { name: "COP Model" })).toHaveCount(0);
        await expect(page.getByRole("button", { name: /COP/ })).toHaveCount(0);
        const retirement = page.getByRole("heading", { name: "COP retired" }).locator("..");
        await expect(retirement).toContainText("not measured COP");
        await expect(retirement).toContainText("Totalt COP-värde");
        await expect(retirement).toContainText("Ungefär");
        await expect(retirement).toContainText("not an optimizer input");

        const demandCard = page.locator(".model-card").filter({ has: page.getByRole("heading", { name: "Demand Model" }) });
        await expect(demandCard).toContainText(/Last trained: .*2026/);
        await expect(demandCard).toContainText("42 energy readings");
        await expect(demandCard).toContainText("validation MAE 0.456 kW");
    });

    test("shows saved-model integrity failures on the affected model card", async ({ page }) => {
        await mockOptimizerStatus(page, {
            ...statusWithFreshData,
            demand_model: { ...statusWithFreshData.demand_model, unavailable_reason: "integrity_check_failed" },
        });
        await page.goto("/?view=under-the-hood");
        await page.getByRole("button", { name: "Show diagnostics" }).click();

        const demandCard = page.locator(".model-card").filter({ has: page.getByRole("heading", { name: "Demand Model" }) });
        await expect(demandCard).toContainText("Saved model failed its integrity check.");
    });

    test("has a no-skip heading hierarchy in the accessibility snapshot", async ({ page }) => {
        await page.goto("/?view=under-the-hood");
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        const snapshot = await page.locator("#dashboard-panel-under-the-hood").ariaSnapshot();
        const levels = [...snapshot.matchAll(/\[level=(\d)\]/g)].map((match) => Number(match[1]));
        expect(levels[0]).toBe(2);
        expect(levels.every((level, index) => index === 0 || level <= levels[index - 1] + 1)).toBe(true);
    });

    test("routes training controls only to their matching POST endpoints", async ({ page }) => {
        await mockOptimizerStatus(page);
        const postPaths: string[] = [];
        page.on("request", (request) => {
            if (request.method() === "POST" && request.url().includes("/api/")) {
                postPaths.push(new URL(request.url()).pathname);
            }
        });

        await page.goto("/?view=under-the-hood");
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        await page.getByRole("button", { name: "Train Demand" }).click();
        await expect.poll(() => postPaths).toContain("/api/ml/train");
        await page.getByRole("button", { name: "Train Comfort Model" }).click();
        await expect.poll(() => postPaths).toContain("/api/comfort-model/train");
        await page.getByRole("button", { name: "Calibrate Thermal" }).click();
        await expect.poll(() => postPaths).toContain("/api/thermal/calibrate");
        expect(postPaths.filter((path) => path.includes("train") || path.includes("calibrate"))).toEqual([
            "/api/ml/train",
            "/api/comfort-model/train",
            "/api/thermal/calibrate",
        ]);
    });

    test("uses ordered Models headings and no horizontal overflow", async ({ page }) => {
        for (const width of [375, 640, 1280]) {
            await page.setViewportSize({ width, height: 800 });
            await page.goto("/?view=under-the-hood");
            await page.getByRole("button", { name: "Show diagnostics" }).click();
            const levels = await page.locator("#dashboard-panel-under-the-hood h1, #dashboard-panel-under-the-hood h2, #dashboard-panel-under-the-hood h3, #dashboard-panel-under-the-hood h4").evaluateAll((headings) => headings.map((heading) => Number(heading.tagName.slice(1))));
            await expect(page.locator(".model-grid")).not.toContainText("undefined");
            await expect(page.locator(".model-grid")).toContainText("electrical space-heating demand");
            expect(levels.every((level, index) => index === 0 || level <= levels[index - 1] + 1)).toBe(true);
            expect(await page.evaluate(() => document.body.scrollWidth)).toBeLessThanOrEqual(width + 1);
            await expect(page.getByRole("heading", { name: "COP Model" })).toHaveCount(0);
            await expect(page.getByRole("button", { name: /COP/ })).toHaveCount(0);
            if (width === 375 || width === 1280) {
                await page.screenshot({ path: test.info().outputPath(`cop-retirement-${width}.png`), fullPage: true });
            }
        }
    });

    for (const usableSamples of [0, 50, 167, 168]) {
        test(`Demand training requires 168 usable intervals: ${usableSamples}`, async ({ page }) => {
            await mockOptimizerStatus(page, {
                ...statusWithFreshData,
                cop_model: { ...retiredCopModel, trained: true },
                demand_model: {
                    ...statusWithFreshData.demand_model,
                    data_quality: { ...demandDataQuality, usable_samples: usableSamples, ready_to_train: true },
                },
            });
            await page.goto("/?view=under-the-hood");
            await page.getByRole("button", { name: "Show diagnostics" }).click();
            const button = page.getByRole("button", { name: "Train Demand", exact: true });
            if (usableSamples < 168) await expect(button).toBeDisabled();
            else await expect(button).toBeEnabled();
            await expect(page.getByRole("button", { name: /COP/ })).toHaveCount(0);
        });
    }

    test("Demand training also requires its own readiness flag", async ({ page }) => {
        await mockOptimizerStatus(page, {
            ...statusWithFreshData,
            demand_model: {
                ...statusWithFreshData.demand_model,
                data_quality: { ...demandDataQuality, usable_samples: 168, ready_to_train: false },
            },
        });
        await page.goto("/?view=under-the-hood");
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        await expect(page.getByRole("button", { name: "Train Demand", exact: true })).toBeDisabled();
    });

    test("Demand success ignores the COP retirement error and refreshes without selectors", async ({ page }) => {
        let trained = false;
        const requests: Array<{ path: string; query: string; body: string | null }> = [];
        await page.route("**/api/optimizer/status", (route) => route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
                ...statusWithFreshData,
                demand_model: { ...statusWithFreshData.demand_model, trained },
            }),
        }));
        await page.route("**/api/ml/train", (route) => {
            const request = route.request();
            const url = new URL(request.url());
            requests.push({ path: url.pathname, query: url.search, body: request.postData() });
            trained = true;
            return route.fulfill({
                status: 200, contentType: "application/json", body: JSON.stringify({
                    cop: { status: "retired", supported: false, error: "cop_model_retired" },
                    demand: { version: "fixture-demand" },
                })
            });
        });
        await page.goto("/?view=under-the-hood");
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        await page.getByRole("button", { name: "Train Demand", exact: true }).click();
        await expect(page.locator(".train-msg--success")).toHaveText("Demand model trained.");
        const demandCard = page.locator(".model-card").filter({ has: page.getByRole("heading", { name: "Demand Model" }) });
        await expect(demandCard.getByText("Validated", { exact: true })).toBeVisible();
        await expect(page.getByRole("heading", { name: "COP retired" })).toBeVisible();
        await expect(page.getByRole("button", { name: /COP/ })).toHaveCount(0);
        expect(requests).toEqual([{ path: "/api/ml/train", query: "", body: null }]);
    });

    test("Demand failure cannot claim training success or activate COP after refresh", async ({ page }) => {
        await mockOptimizerStatus(page);
        await page.route("**/api/ml/train", (route) => route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({ cop: { error: "cop_model_retired" }, demand: { error: "Insufficient data", samples: 167 } }),
        }));
        await page.goto("/?view=under-the-hood");
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        await page.getByRole("button", { name: "Train Demand", exact: true }).click();
        await expect(page.locator(".train-msg--error")).toHaveText("Demand: Insufficient data (167 samples)");
        await expect(page.locator(".train-msg--success")).toHaveCount(0);
        await expect(page.getByRole("heading", { name: "COP retired" })).toBeVisible();
        await expect(page.getByRole("button", { name: "Train Demand", exact: true })).toBeEnabled();
    });

    test("Demand HTTP failure remains visible and restores the training control", async ({ page }) => {
        await mockOptimizerStatus(page);
        await page.route("**/api/ml/train", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Demand training unavailable" }) }));
        await page.goto("/?view=under-the-hood");
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        await page.getByRole("button", { name: "Train Demand", exact: true }).click();
        await expect(page.locator(".train-msg--error")).toHaveText("Demand training unavailable");
        await expect(page.getByRole("button", { name: "Train Demand", exact: true })).toBeEnabled();
        await expect(page.getByRole("button", { name: /COP/ })).toHaveCount(0);
    });

    test("shows the required status error when optimizer status fails", async ({ page }) => {
        await page.route("**/api/optimizer/status", (route) => route.fulfill({ status: 503, body: "{}" }));
        await page.goto("/?view=under-the-hood");
        await expect(page.getByText("Failed to load optimizer status")).toBeVisible();
    });

    test("preserves control focus on first load and reload", async ({ page }) => {
        let releaseStatus: (() => void) | undefined;
        let statusReady = Promise.resolve();
        await page.route("**/api/optimizer/status", async (route) => {
            await statusReady;
            return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(statusWithFreshData) });
        });
        for (const navigation of ["first load", "reload"]) {
            statusReady = new Promise<void>((resolve) => {
                releaseStatus = resolve;
            });
            if (navigation === "first load") await page.goto("/?view=under-the-hood");
            else await page.reload();
            await expect(page.getByRole("heading", { name: "How the optimizer is deciding" })).toBeVisible();
            const tab = page.getByRole("tab", { name: "Under the hood" });
            await tab.focus();
            expect(releaseStatus).toBeDefined();
            releaseStatus?.();
            const heading = page.getByRole("heading", { name: "Why it decides" });
            await expect(heading).toBeVisible();
            await expect(tab).toBeFocused();
            await expect(heading).not.toBeFocused();
        }
    });

    test("preserves control focus during Demand training refresh", async ({ page }) => {
        await mockOptimizerStatus(page);
        let releaseTraining: (() => void) | undefined;
        const trainingReady = new Promise<void>((resolve) => {
            releaseTraining = resolve;
        });
        await page.route("**/api/ml/train", async (route) => {
            await trainingReady;
            return route.fulfill({
                status: 200, contentType: "application/json", body: JSON.stringify({
                    cop: { status: "retired", supported: false, error: "cop_model_retired" },
                    demand: { version: "fixture-demand" },
                })
            });
        });
        await page.goto("/?view=under-the-hood");
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        const trainingRequest = page.waitForRequest("**/api/ml/train");
        await page.getByRole("button", { name: "Train Demand", exact: true }).click();
        await trainingRequest;
        const tab = page.getByRole("tab", { name: "Under the hood" });
        await tab.focus();
        const refreshedStatus = page.waitForResponse((response) =>
            response.url().includes("/api/optimizer/status") && response.status() === 200,
        );
        expect(releaseTraining).toBeDefined();
        releaseTraining?.();
        await refreshedStatus;
        await expect(page.locator(".train-msg--success")).toHaveText("Demand model trained.");
        await expect(page.getByRole("button", { name: "Train Demand", exact: true })).toBeEnabled();
        await expect(tab).toBeFocused();
        await expect(page.getByRole("heading", { name: "Why it decides" })).not.toBeFocused();
    });

    for (const retryStatus of [200, 503]) {
        test(`preserves other control focus after a ${retryStatus} retry`, async ({ page }) => {
            let allowRetry = false;
            let releaseRetry: (() => void) | undefined;
            const retryReady = new Promise<void>((resolve) => {
                releaseRetry = resolve;
            });
            await page.route("**/api/optimizer/status", async (route) => {
                if (!allowRetry) return route.fulfill({ status: 503, body: "{}" });
                await retryReady;
                return route.fulfill({ status: retryStatus, contentType: "application/json", body: JSON.stringify(statusWithFreshData) });
            });
            await page.goto("/?view=under-the-hood");
            await expect(page.getByText("Failed to load optimizer status")).toBeVisible();
            allowRetry = true;
            await page.getByRole("button", { name: "Retry", exact: true }).click();
            await expect(page.getByRole("button", { name: "Retrying..." })).toHaveAttribute("aria-busy", "true");
            const tab = page.getByRole("tab", { name: "Under the hood" });
            await tab.focus();
            expect(releaseRetry).toBeDefined();
            releaseRetry?.();
            const heading = page.getByRole("heading", { name: "Why it decides" });
            if (retryStatus === 200) await expect(heading).toBeVisible();
            else {
                await expect(page.getByText("Failed to load optimizer status")).toBeVisible();
                await expect(page.getByRole("button", { name: "Retry", exact: true })).toBeEnabled();
                await expect(heading).toHaveCount(0);
            }
            await expect(tab).toBeFocused();
            await expect.poll(() => page.evaluate(() => document.activeElement?.id)).not.toBe("why-it-decides-heading");
        });
    }

    test("focuses retry recovery after a delayed render", async ({ page }) => {
        await page.addInitScript(() => {
            const scheduler = { held: false, pending: [] as Array<() => void> };
            Object.defineProperty(window, "retryFocusScheduler", { value: scheduler });
            const NativeMessageChannel = window.MessageChannel;
            const onmessage = Object.getOwnPropertyDescriptor(MessagePort.prototype, "onmessage")!;
            window.MessageChannel = class extends NativeMessageChannel {
                constructor() {
                    super();
                    const port = this.port1;
                    Object.defineProperty(port, "onmessage", {
                        get: () => onmessage.get!.call(port),
                        set: (handler: ((event: MessageEvent) => void) | null) => {
                            onmessage.set!.call(port, handler && ((event: MessageEvent) => {
                                const deliver = () => handler.call(port, event);
                                if (scheduler.held) scheduler.pending.push(deliver);
                                else deliver();
                            }));
                        },
                    });
                }
            };
        });
        let allowSuccess = false;
        let releaseRetry: (() => void) | undefined;
        await page.route("**/api/optimizer/status", async (route) => {
            if (!allowSuccess) return route.fulfill({ status: 503, body: "{}" });
            await new Promise<void>((resolve) => {
                releaseRetry = resolve;
            });
            return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(statusWithFreshData) });
        });
        await page.goto("/?view=under-the-hood");
        await expect(page.getByText("Failed to load optimizer status")).toBeVisible();
        allowSuccess = true;
        await page.getByRole("button", { name: "Retry", exact: true }).click();
        await expect(page.getByRole("button", { name: "Retrying..." })).toHaveAttribute("aria-busy", "true");
        expect(releaseRetry).toBeDefined();
        await page.evaluate(() => {
            (window as typeof window & { retryFocusScheduler: { held: boolean } }).retryFocusScheduler.held = true;
        });
        releaseRetry?.();
        await page.waitForFunction(() =>
            (window as typeof window & { retryFocusScheduler: { pending: unknown[] } }).retryFocusScheduler.pending.length > 0,
        );
        await page.evaluate(() => new Promise<void>((resolve) => {
            requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
        }));
        const heading = page.getByRole("heading", { name: "Why it decides" });
        await expect(heading).toHaveCount(0);
        await page.evaluate(() => {
            const scheduler = (window as typeof window & {
                retryFocusScheduler: { held: boolean; pending: Array<() => void> };
            }).retryFocusScheduler;
            scheduler.held = false;
            for (const deliver of scheduler.pending.splice(0)) deliver();
        });
        await expect(heading).toBeVisible();
        await expect(heading).toBeFocused();
    });

    test("offers retry after the required status endpoint fails", async ({ page }) => {
        let statusCalls = 0;
        let allowSuccess = false;
        let releaseRetry: (() => void) | undefined;
        await page.route("**/api/optimizer/status", async (route) => {
            statusCalls += 1;
            if (!allowSuccess) return route.fulfill({ status: 503, body: "{}" });
            await new Promise<void>((resolve) => {
                releaseRetry = resolve;
            });
            return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(statusWithFreshData) });
        });
        await page.goto("/?view=under-the-hood");
        await expect(page.getByText("Failed to load optimizer status")).toBeVisible();
        allowSuccess = true;
        await page.getByRole("button", { name: "Retry", exact: true }).click();
        await expect(page.getByRole("button", { name: "Retrying..." })).toHaveAttribute("aria-busy", "true");
        expect(releaseRetry).toBeDefined();
        const retryResponse = page.waitForResponse((response) =>
            response.url().includes("/api/optimizer/status") && response.status() === 200,
        );
        releaseRetry?.();
        await retryResponse;
        const decisionHeading = page.getByRole("heading", { name: "Why it decides" });
        await expect(decisionHeading).toBeVisible();
        await expect(decisionHeading).toBeFocused();
        expect(statusCalls).toBeGreaterThan(1);
    });
});