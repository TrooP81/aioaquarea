import { expect, manualCOPObservation, manualCOPResponse, test, type Page } from "./fixtures";

type Row = ReturnType<typeof manualCOPObservation> & { scope: string };

function unknownScope(row: ReturnType<typeof manualCOPObservation>): Row {
    return { ...row, scope: "unknown" };
}

interface Ledger {
    rows: Row[];
    writes: Array<{ method: string; path: string; body: Record<string, unknown> }>;
    getStatus: number;
    monitoring: Record<string, unknown> | null;
    nextRevision: string;
}

async function ledger(page: Page, rows: Row[] = []): Promise<Ledger> {
    const state: Ledger = { rows, writes: [], getStatus: 200, monitoring: null, nextRevision: "9007199254740993" };
    await page.route("**/api/cop/manual**", async (route) => {
        const request = route.request();
        const url = new URL(request.url());
        if (request.method() === "GET") {
            if (state.getStatus !== 200) {
                return route.fulfill({
                    status: state.getStatus, contentType: "application/json",
                    body: JSON.stringify({ detail: "Manual COP stored period is invalid" }),
                });
            }
            const device = url.searchParams.get("device_id");
            const series = url.searchParams.get("granularity") === "month" ? "month" : "day";
            return route.fulfill({
                status: 200, contentType: "application/json", body: JSON.stringify({
                    ...manualCOPResponse, device_id: device, granularity: series,
                    observations: device ? state.rows.filter((row) => row.granularity === series) : [],
                    forecasting_enabled: state.monitoring !== null,
                    monitoring: state.monitoring ?? manualCOPResponse.monitoring,
                }),
            });
        }
        const body = request.postDataJSON();
        state.writes.push({ method: request.method(), path: url.pathname, body });
        const [, , , , , series, periodDate] = url.pathname.split("/");
        const existing = state.rows.find((row) => row.granularity === series && row.period_date === periodDate);
        if (body.expected_revision !== (existing?.revision ?? "0")) return route.fulfill({ status: 409, body: "{}" });
        if (request.method() === "DELETE") {
            state.rows = state.rows.filter((row) => row !== existing);
            return route.fulfill({ status: 204 });
        }
        const saved = {
            ...unknownScope(manualCOPObservation(periodDate, body.value, state.nextRevision, series as "day" | "month")),
            observed_at: body.observed_at, completeness: body.completeness, timezone: body.timezone,
        };
        state.rows = state.rows.filter((row) => row !== existing).concat(saved);
        return route.fulfill({ status: existing ? 200 : 201, contentType: "application/json", body: JSON.stringify(saved) });
    });
    return state;
}

async function openPanel(page: Page) {
    await page.goto("/?view=under-the-hood");
    await page.getByRole("button", { name: "Show diagnostics" }).click();
    const panel = page.locator('section[aria-labelledby="manual-cop-heading"]');
    await panel.getByRole("combobox", { name: "Device", exact: true }).selectOption("fixture-device");
    await expect(panel.getByRole("button", { name: "Refresh readings" })).toBeEnabled();
    return panel;
}

test.describe("Manual controller COP local-calendar contract", () => {
    test("labels the measurement scope as unknown and never as total", async ({ page }) => {
        await ledger(page, [unknownScope(manualCOPObservation())]);
        const panel = await openPanel(page);
        const provenance = panel.locator(".manual-provenance");
        await expect(provenance).toContainText("Scope: unknown");
        await expect(provenance).toContainText("Hot-water inclusion: user-reported, unverified");
        await expect(provenance).toContainText("Measured: no");
        await expect(provenance).toContainText("Energy boundary: unknown");
        await expect(provenance.getByText("Total", { exact: true })).toHaveCount(0);
        await expect(panel.locator(".manual-reading")).toHaveCount(1);
    });

    test("stores local day and month identities and never sends boundary instants", async ({ page }) => {
        const state = await ledger(page);
        const panel = await openPanel(page);
        await panel.getByLabel("Period date").fill("2026-10-04");
        await panel.getByLabel("COP value").fill("9.7");
        await panel.getByLabel("Observed at (UTC)").fill("2026-10-04T08:00");
        await panel.getByLabel("Controller timezone confirmed").check();
        await panel.getByRole("button", { name: "Save reading" }).click();
        await expect(panel.locator(".manual-value")).toHaveText("9.7");

        await panel.getByRole("button", { name: "Month", exact: true }).click();
        await panel.getByLabel("Period month").fill("2026-10");
        await panel.getByLabel("COP value").fill("3.9");
        await panel.getByLabel("Observed at (UTC)").fill("2026-10-04T08:00");
        await panel.getByLabel("Controller timezone confirmed").check();
        await panel.getByRole("button", { name: "Save reading" }).click();
        await expect(panel.locator(".manual-value")).toHaveText("3.9");

        expect(state.writes.map((write) => write.path)).toEqual([
            "/api/cop/manual/fixture-device/day/2026-10-04",
            "/api/cop/manual/fixture-device/month/2026-10-01",
        ]);
        for (const write of state.writes) {
            expect(Object.keys(write.body).sort()).toEqual(["completeness", "expected_revision", "observed_at", "timezone", "value"]);
            expect(write.body.observed_at).toBe("2026-10-04T08:00:00.000Z");
            expect(write.body.timezone).toBe("Europe/Stockholm");
            expect(write.body.expected_revision).toBe("0");
        }
    });

    test("keeps decimal-string revisions exact across correction and stale-token conflict", async ({ page }) => {
        const state = await ledger(page, [unknownScope(manualCOPObservation("2026-10-04", 9.7, "9007199254740993"))]);
        const panel = await openPanel(page);
        state.nextRevision = "9007199254740995";
        await panel.getByRole("button", { name: "Edit reading 2026-10-04" }).click();
        await panel.getByLabel("COP value").fill("5.5");
        await panel.getByRole("button", { name: "Save correction" }).click();
        await expect(panel.locator(".manual-value")).toHaveText("5.5");
        expect(state.writes[0].body.expected_revision).toBe("9007199254740993");

        state.rows = [unknownScope(manualCOPObservation("2026-10-04", 6.1, "9007199254740997"))];
        page.once("dialog", (dialog) => dialog.accept());
        await panel.getByRole("button", { name: "Delete reading 2026-10-04" }).click();
        await expect(panel.getByRole("status")).toContainText("Current readings refreshed");
        expect(state.writes[1].body).toEqual({ expected_revision: "9007199254740995" });
        await expect(panel.locator(".manual-value")).toHaveText("6.1");
    });

    test("fails closed when stored calendar data is invalid", async ({ page }) => {
        const state = await ledger(page, [unknownScope(manualCOPObservation())]);
        state.monitoring = {
            ...manualCOPResponse.monitoring, status: "unavailable", reason: "invalid_stored_period",
            target_date: null, forecast: null,
        };
        const panel = await openPanel(page);
        await expect(panel.getByTestId("manual-forecast")).toContainText("Forecast unavailable");
        await expect(panel.getByTestId("manual-forecast")).toContainText("invalid stored period");
        await expect(panel.getByTestId("manual-projection")).toHaveCount(0);

        state.getStatus = 503;
        await panel.getByRole("button", { name: "Refresh readings" }).click();
        await expect(panel.getByRole("alert")).toHaveText("Manual readings unavailable (503)");
        await expect(panel.getByTestId("manual-projection")).toHaveCount(0);
        await expect(panel.getByTestId("manual-forecast")).toContainText("Forecast unavailable");
    });
});
