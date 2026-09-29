"use client";

import type { ReactNode } from "react";

export function Banner({
    tone,
    children,
}: {
    tone: "info" | "warning" | "danger";
    children: ReactNode;
}) {
    return (
        <div className={`banner banner--${tone}`} role={tone === "danger" ? "alert" : "status"}>
            {children}
        </div>
    );
}