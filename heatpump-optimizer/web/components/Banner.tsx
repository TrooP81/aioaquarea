"use client";

import type { ReactNode } from "react";

export function Banner({
    tone,
    label,
    children,
}: {
    tone: "info" | "warning" | "danger";
    label?: string;
    children: ReactNode;
}) {
    return (
        <div className={`banner banner--${tone}`} role={tone === "danger" ? "alert" : "status"} aria-label={label}>
            {children}
        </div>
    );
}