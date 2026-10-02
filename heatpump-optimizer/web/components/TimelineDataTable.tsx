"use client";

import { useCurrency, formatPricePerKwh } from "./useCurrency";
import { useTimeFormat, formatTime } from "./useTimeFormat";
import { ACTION_LABELS, STATUS_DISPLAY } from "@/lib/constants";
import { actionReason, type TimelineAction, type TimelinePoint } from "@/lib/timeline-data";

export function TimelineDataTable({ points, actions }: { points: TimelinePoint[]; actions: TimelineAction[] }) {
    const currency = useCurrency();
    const time = useTimeFormat();
    return <section className="plan-section timeline-table-section"><h2 className="chart-title">Timeline data</h2><div className="timeline-table-scroll"><table><thead><tr><th>Time</th><th>Actual</th><th>Forecast</th><th>Comfort range</th><th>Target</th><th>Price</th><th>Action</th><th>Status</th><th>Reason</th></tr></thead><tbody>{points.map((point) => { const matching = actions.find((action) => Math.abs(new Date(action.executed_at || action.scheduled_ts).getTime() - point.timestamp) < 1_800_000); const status = matching ? STATUS_DISPLAY[matching.status] || { text: matching.status } : null; return <tr key={point.timestamp}><td>{formatTime(new Date(point.timestamp), time.hour12)}</td><td>{point.actual?.toFixed(1) ?? "-"}</td><td>{point.forecast?.toFixed(1) ?? "-"}</td><td>{point.comfortMin != null ? `${point.comfortMin.toFixed(1)}-${point.comfortMax?.toFixed(1)}` : "-"}</td><td>{point.target?.toFixed(1) ?? "-"}</td><td>{formatPricePerKwh(point.price, currency)}</td><td>{matching ? ACTION_LABELS[matching.action_type]?.label || matching.action_type : "-"}</td><td>{status?.text ?? "-"}</td><td>{matching ? actionReason(matching) : "-"}</td></tr>; })}</tbody></table></div></section>;
}