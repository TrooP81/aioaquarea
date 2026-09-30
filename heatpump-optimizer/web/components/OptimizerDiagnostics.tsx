"use client";

import { useState, type ReactNode } from "react";

export function OptimizerDiagnostics({ children }: { children: ReactNode }) {
    const [open, setOpen] = useState(false);
    return (
        <section aria-labelledby="optimizer-diagnostics-heading" className="optimizer-diagnostics-section">
            <div className="optimizer-diagnostics-header">
                <h2 id="optimizer-diagnostics-heading" className="chart-title">Diagnostics</h2>
                <button type="button" className="btn btn-sm" aria-expanded={open} aria-controls="optimizer-diagnostics-panel" onClick={() => setOpen((value) => !value)}>{open ? "Hide diagnostics" : "Show diagnostics"}</button>
            </div>
            {open && <div id="optimizer-diagnostics-panel" className="optimizer-diagnostics-panel">{children}</div>}
        </section>
    );
}