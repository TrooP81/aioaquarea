import type { ControlState } from "@/lib/api-types";

interface ReadinessModel {
    label: string;
    plain: string;
    nextStep: string;
}

interface WhyItDecidesProps {
    controlState: ControlState | null;
    activeLayer: string;
    fallbackActive: boolean;
    limitations: string[];
    models: ReadinessModel[];
}

export function WhyItDecides({ controlState, activeLayer, fallbackActive, limitations, models }: WhyItDecidesProps) {
    return (
        <section aria-labelledby="why-it-decides-heading">
            <h2 id="why-it-decides-heading" className="chart-title" tabIndex={-1}>Why it decides</h2>
            <p className="optimizer-readable-text">The active engine is <strong>{activeLayer}</strong>. It uses the safest available decision layer for the current evidence.</p>
            {fallbackActive && <p className="optimizer-readable-text">The last optimized plan fell back to rules.</p>}
            <p className="optimizer-readable-text">{controlState ? `${controlState.headline} ${controlState.detail}` : "Control state unavailable."}</p>
            {limitations.map((limitation) => <p className="optimizer-readable-text" key={limitation}>{limitation}</p>)}
            {models.map((model) => <p className="optimizer-readable-text" key={model.label}><strong>{model.label}:</strong> {model.plain}</p>)}
        </section>
    );
}