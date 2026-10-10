import { expect, test } from "./fixtures";

const now = new Date().toISOString();

const dashboard = {
    current_status: {
        ts: now,
        device_id: "test-device",
        mode: "heat",
        operation_status: 1,
        outdoor_temp: 5,
        tank_temp: 48,
        tank_target_temp: 50,
        zone1_temp: 21,
        zone1_target_temp: 22,
        quiet_mode: 0,
        powerful_mode: 0,
    },
    current_price: 0.085,
    today_kwh: 12.5,
    today_cost_eur: 1.23,
    active_plan: null,
    has_override: false,
};

const optimizerStatus = {
    configured_layer: "rules_only",
    active_layer: "rules_v3",
    fallback_layer: "rules_v3",
    learning_mode: { enabled: false, since: null, days_elapsed: null },
    cop_model: { trained: false, last_trained: null, samples: 0 },
    demand_model: { trained: false, last_trained: null, samples: 0 },
    thermal_model: { calibrated: false, tank_heating_rate: 0, confidence: "default", last_calibrated: null },
};

const comfortStatus = {
    trained: false,
    training_samples: 0,
    last_trained: null,
    is_ready_for_control: false,
    direct_forecast_horizons_minutes: [],
};

async function mockPage(page: import("@playwright/test").Page, trainResponse: object) {
    await page.route("**/api/**", (route) =>
        route.fulfill({ status: 404, contentType: "application/json", body: "{}" })
    );
    await page.route("**/api/dashboard", (route) =>
        route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(dashboard) })
    );
    await page.route("**/api/optimizer/status", (route) =>
        route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(optimizerStatus) })
    );
    await page.route("**/api/comfort-model/status", (route) =>
        route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(comfortStatus) })
    );
    await page.route("**/api/comfort-model/train", (route) =>
        route.fulfill({ status: "status" in trainResponse && trainResponse.status === "queued" ? 202 : 200, contentType: "application/json", body: JSON.stringify(trainResponse) })
    );
}

test.describe("Comfort training messages", () => {
    test("queued maintenance shows durable IDs without claiming a fitted model", async ({ page }) => {
        await mockPage(page, { status: "queued", request_id: "request-comfort", maintenance_enabled: false });
        await page.route("**/api/optimizer/status", (route) => route.fulfill({
            status: 200, contentType: "application/json",
            body: JSON.stringify({
                ...optimizerStatus,
                demand_model: {
                    ...optimizerStatus.demand_model,
                    data_quality: {
                        total_records: 169, total_intervals: 168, usable_samples: 168,
                        minimum_samples: 168, remaining_samples: 0, ready_to_train: true,
                        rejected_missing: 0, rejected_reset: 0, rejected_heating_inactive: 0,
                        rejected_nonpositive: 0, rejected_rate_bounds: 0, weather_matches: 168,
                    },
                },
            }),
        }));
        await page.route("**/api/ml/train", (route) => route.fulfill({
            status: 202, contentType: "application/json",
            body: JSON.stringify({
                cop: { status: "retired", supported: false, error: "cop_model_retired", reason: "unverified_thermal_output" },
                demand: { status: "queued", request_id: "request-demand", maintenance_enabled: false },
            }),
        }));
        await page.route("**/api/thermal/calibrate", (route) => route.fulfill({
            status: 202, contentType: "application/json",
            body: JSON.stringify({ status: "queued", request_id: "request-thermal", maintenance_enabled: false }),
        }));
        await page.goto("/");
        await page.getByRole("tab", { name: "Under the hood" }).click();
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        for (const [button, component] of [["Train Demand", "Demand"], ["Train Comfort Model", "Comfort"], ["Calibrate Thermal", "Thermal"]]) {
            await page.getByRole("button", { name: button, exact: true }).click();
            await expect(page.locator(".train-msg")).toHaveText(`${component} maintenance queued (request-${component.toLowerCase()}). Maintenance disabled.`);
            await expect(page.locator(".train-msg")).toHaveClass(/train-msg--info/);
            await expect(page.locator(".train-msg")).not.toContainText(/model trained|model calibrated/);
        }
    });

    test("legacy control readiness remains reporting only", async ({ page }) => {
        await mockPage(page, { status: "trained", control_ready: true, control_readiness: { reason: "ready_for_control" } });
        await page.route("**/api/comfort-model/status", (route) => route.fulfill({
            contentType: "application/json",
            body: JSON.stringify({ ...comfortStatus, trained: true, control_ready: true, metrics: { active_heating_rows: 20 } }),
        }));
        await page.goto("/");
        await page.getByRole("tab", { name: "Under the hood" }).click();
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        const card = page.locator(".model-card").filter({ has: page.getByRole("heading", { name: "Comfort Model", exact: true }) });
        await expect(card).toContainText("Trained for reporting only");
        await expect(card).toContainText("Optimization requires a new reviewed, approved build");
        await page.getByRole("button", { name: "Train Comfort Model" }).click();
        await expect(page.locator(".train-msg")).toContainText("reporting only");
        await expect(page.locator(".train-msg")).toContainText("Ordinary writes remain prohibited");
        await expect(page.locator(".train-msg")).not.toContainText(/ready for control|approved for control/i);
        await expect(page.getByText("Comfort model trained and approved for indoor-temperature control.")).toHaveCount(0);
    });

    test("shows in-progress message without approved success", async ({ page }) => {
        await mockPage(page, { status: "training_in_progress" });

        await page.goto("/");
        await page.getByRole("tab", { name: "Under the hood" }).click();
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        await page.getByRole("button", { name: "Train Comfort Model" }).click();

        await expect(page.locator(".train-msg")).toHaveText("Training already in progress");
        await expect(page.getByText("Comfort model trained and approved for indoor-temperature control.")).toHaveCount(0);
    });

    test("shows skipped reason without approved success", async ({ page }) => {
        await mockPage(page, { status: "training_skipped", reason: "insufficient_samples" });

        await page.goto("/");
        await page.getByRole("tab", { name: "Under the hood" }).click();
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        await page.getByRole("button", { name: "Train Comfort Model" }).click();

        await expect(page.locator(".train-msg")).toHaveText("Training skipped: insufficient_samples");
        await expect(page.locator(".train-msg")).toHaveClass(/train-msg--error/);
        await expect(page.getByText("Comfort model trained and approved for indoor-temperature control.")).toHaveCount(0);
    });

    test("shows recovery notice without approved success", async ({ page }) => {
        await mockPage(page, { training_notice: "training_lock_finalize_recovered" });

        await page.goto("/");
        await page.getByRole("tab", { name: "Under the hood" }).click();
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        await page.getByRole("button", { name: "Train Comfort Model" }).click();

        await expect(page.locator(".train-msg")).toHaveText("Training completed: lock finalization recovered");
        await expect(page.locator(".train-msg")).toHaveClass(/train-msg--success/);
        await expect(page.getByText("Comfort model trained and approved for indoor-temperature control.")).toHaveCount(0);
    });
});