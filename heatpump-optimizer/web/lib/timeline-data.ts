export interface TimelineAction {
    id: number;
    plan_id?: number;
    scheduled_ts: string;
    action_type: string;
    payload: Record<string, unknown>;
    status: string;
    executed_at?: string | null;
    result?: Record<string, unknown> | null;
}

export interface TimelinePoint {
    timestamp: number;
    actual?: number;
    forecast?: number;
    comfortMin?: number;
    comfortMax?: number;
    target?: number;
    price?: number;
}

interface ForecastData {
    forecast_with_plan?: Array<{ hour: number; ts?: string | null; predicted_indoor_temp?: number | null }>;
    target_schedule?: Array<{ hour: number; ts?: string | null; target?: number | null }>;
}

function timestamp(value: string | null | undefined): number | undefined {
    if (!value) return undefined;
    const parsed = new Date(value).getTime();
    return Number.isNaN(parsed) ? undefined : parsed;
}

function pointFor(points: Map<number, TimelinePoint>, value: number): TimelinePoint {
    const bucket = Math.round(value / 300_000) * 300_000;
    const existing = points.get(bucket);
    if (existing) return existing;
    const point = { timestamp: bucket };
    points.set(bucket, point);
    return point;
}

export function buildTimelineData({
    now = Date.now(),
    prices = [],
    indoor = [],
    forecast,
    comfortMin,
    comfortMax,
}: {
    now?: number;
    prices?: Array<{ ts: string; price_eur_per_kwh?: number | null }>;
    indoor?: Array<{ timestamp: string; temperature?: number | null }>;
    forecast?: ForecastData | null;
    comfortMin?: number;
    comfortMax?: number;
}): TimelinePoint[] {
    const start = now - 24 * 3_600_000;
    const end = now + 24 * 3_600_000;
    const points = new Map<number, TimelinePoint>();
    const include = (value: number | undefined) => value != null && value >= start && value <= end;

    prices.forEach((price) => {
        const value = timestamp(price.ts);
        if (value == null || !include(value)) return;
        pointFor(points, value).price = price.price_eur_per_kwh ?? undefined;
    });
    indoor.forEach((reading) => {
        const value = timestamp(reading.timestamp);
        if (value == null || !include(value)) return;
        pointFor(points, value).actual = reading.temperature ?? undefined;
    });

    const forecastStart = forecast?.forecast_with_plan
        ?.map((point) => timestamp(point.ts))
        .find((value): value is number => value != null) ?? now;
    forecast?.forecast_with_plan?.forEach((reading) => {
        const value = timestamp(reading.ts) ?? forecastStart + reading.hour * 3_600_000;
        if (!include(value)) return;
        const point = pointFor(points, value);
        point.forecast = reading.predicted_indoor_temp ?? undefined;
        point.comfortMin = comfortMin;
        point.comfortMax = comfortMax;
    });
    forecast?.target_schedule?.forEach((target) => {
        const value = timestamp(target.ts) ?? forecastStart + target.hour * 3_600_000;
        if (!include(value)) return;
        pointFor(points, value).target = target.target ?? undefined;
    });

    return [...points.values()].sort((left, right) => left.timestamp - right.timestamp);
}

export function actionReason(action: TimelineAction): string {
    const result = action.result;
    for (const key of ["detail", "error", "reason"]) {
        const value = result?.[key];
        if (typeof value === "string" && value.trim()) return value.replace(/_/g, " ");
    }
    if (action.status === "executed" || action.status === "executed_unverified") return "Command completed";
    if (action.status === "skipped_peak") return "Skipped because electricity price was at its peak";
    if (action.status === "skipped") return "Skipped by the optimizer";
    if (action.status === "failed") return "Command could not be completed";
    return "Recorded by the optimizer";
}