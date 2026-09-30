import { expect, test } from "./fixtures";

const statusWithFreshData = {
    configured_layer: "rules_only",
    active_layer: "rules_v3",
    fallback_layer: "rules_v3",
    last_plan: { version: "rules_v3", engine: "rules", fell_back: false, created_at: null },
    data_freshness: {
        latest_device_status: "2026-09-30T10:00:00Z",
        age_seconds: 0,
        stale_after_seconds: 300,
        fresh: true,
    },
    planning_data_quality: { control_allowed: true, reasons: [] },
    cop_model: { trained: false, last_trained: null, samples: 12 },
    demand_model: { trained: false, last_trained: null, samples: 8 },
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

async function mockOptimizerStatus(page: import("@playwright/test").Page, status = statusWithFreshData) {
    await page.route("**/api/optimizer/status", (route) =>
        route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(status) }),
    );
}

test.describe("Models status split", () => {
    test("keeps diagnostics collapsed with an accessible keyboard disclosure", async ({ page }) => {
        await page.goto("/?view=status");
        const toggle = page.getByRole("button", { name: "Show diagnostics" });
        await expect(page.getByRole("heading", { name: "Why it decides" })).toBeVisible();
        await expect(toggle).toHaveAttribute("aria-expanded", "false");
        await expect(toggle).toHaveAttribute("aria-controls", "optimizer-diagnostics-panel");
        await expect(page.locator("#optimizer-diagnostics-panel")).toHaveCount(0);
        for (const model of ["COP Model", "Demand Model", "Thermal Model", "Comfort Model"]) {
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
        await page.goto("/?view=status");
        await page.getByRole("button", { name: "Show diagnostics" }).click();

        const undersizedText = await page.locator("#dashboard-panel-status").locator("*").evaluateAll((elements) =>
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
        await page.goto("/?view=status");
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

        await page.goto("/?view=status");
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
            await page.goto(`/?view=status&state=${state}`);
            const summary = page.locator("#dashboard-panel-status");
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
        await page.goto("/?view=status");
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        const diagnostics = page.locator("#optimizer-diagnostics-panel");

        for (const heading of [
            "Decision engine details",
            "Seasonal calibration",
            "Sensor diagnostics",
            "Space-heating gate",
            "Models",
            "Forecast validation",
            "Training",
            "SmartThings indoor temperature",
        ]) {
            await expect(page.getByRole("heading", { name: heading })).toBeVisible();
        }
        for (const field of [
            "COP Model",
            "Demand Model",
            "Thermal Model",
            "Decision engine",
        ]) {
            await expect(diagnostics.getByText(field, { exact: true })).toBeVisible();
        }
        await expect(page.getByRole("heading", { name: "Space-heating gate" })).toBeVisible();
        for (const button of ["Train COP & Demand", "Train Comfort Model", "Calibrate Thermal"]) {
            await expect(page.getByRole("button", { name: button })).toBeVisible();
        }
        const diagnosticsSectionsAreSiblings = await diagnostics.locator("h3").evaluateAll(
            (headings, panel) => headings.every((heading) => heading.closest("section")?.parentElement === panel),
            await diagnostics.elementHandle(),
        );
        expect(diagnosticsSectionsAreSiblings).toBe(true);
    });

    test("has a no-skip heading hierarchy in the accessibility snapshot", async ({ page }) => {
        await page.goto("/?view=status");
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        const snapshot = await page.locator("#dashboard-panel-status").ariaSnapshot();
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

        await page.goto("/?view=status");
        await page.getByRole("button", { name: "Show diagnostics" }).click();
        await page.getByRole("button", { name: "Train COP & Demand" }).click();
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
            await page.goto("/?view=status");
            await page.getByRole("button", { name: "Show diagnostics" }).click();
            const levels = await page.locator("#dashboard-panel-status h1, #dashboard-panel-status h2, #dashboard-panel-status h3, #dashboard-panel-status h4").evaluateAll((headings) => headings.map((heading) => Number(heading.tagName.slice(1))));
            expect(levels.every((level, index) => index === 0 || level <= levels[index - 1] + 1)).toBe(true);
            expect(await page.evaluate(() => document.body.scrollWidth)).toBeLessThanOrEqual(width + 1);
        }
    });

    test("shows the required status error when optimizer status fails", async ({ page }) => {
        await page.route("**/api/optimizer/status", (route) => route.fulfill({ status: 503, body: "{}" }));
        await page.goto("/?view=status");
        await expect(page.getByText("Failed to load optimizer status")).toBeVisible();
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
        await page.goto("/?view=status");
        await expect(page.getByText("Failed to load optimizer status")).toBeVisible();
        allowSuccess = true;
        await page.getByRole("button", { name: "Retry" }).click();
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