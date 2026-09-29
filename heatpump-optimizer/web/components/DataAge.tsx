"use client";

function ageText(timestamp: string | null): string {
    if (!timestamp) return "Unavailable";
    const ageSeconds = Math.max(0, Math.round((Date.now() - new Date(timestamp).getTime()) / 1000));
    if (!Number.isFinite(ageSeconds)) return "Unavailable";
    if (ageSeconds < 60) return "Updated just now";
    if (ageSeconds < 3600) return `Updated ${Math.round(ageSeconds / 60)} minutes ago`;
    return `Updated ${Math.round(ageSeconds / 3600)} hours ago`;
}

export function DataAge({ timestamp, stale = false }: { timestamp: string | null; stale?: boolean }) {
    return <span className={stale ? "data-age data-age--stale" : "data-age"}>{ageText(timestamp)}</span>;
}