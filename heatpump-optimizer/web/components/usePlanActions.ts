"use client";

import { useEffect, useState } from "react";

export interface PlanAction {
  id: number;
  scheduled_ts: string;
  action_type: string;
  payload: Record<string, unknown>;
  status: string;
  executed_at?: string | null;
  result?: Record<string, unknown> | null;
}

export interface PlanOutcome {
  statuses?: Record<string, number>;
  verified_actions?: number;
  timing?: {
    measured_actions?: number;
    on_time_actions?: number;
    average_lateness_seconds?: number | null;
    max_lateness_seconds?: number | null;
  };
  cost_note?: string;
  measurement?: {
    state?: "not_started" | "in_progress" | "completed";
    progress_pct?: number;
    cost?: {
      measured_kwh?: number;
      actual_cost?: number | null;
      coverage_pct?: number;
      estimated_price_shift_savings?: number | null;
    };
    comfort?: {
      samples?: number;
      within_range_pct?: number | null;
      average_c?: number | null;
      minimum_c?: number | null;
    };
    baseline_method?: string;
    note?: string;
  };
}

export interface PlanChangeSummary {
  message?: string;
  compared_to_plan_id?: number;
  drivers?: string[];
}

export interface PlanProvenance {
  generated_at?: string;
  price?: { area?: string; source?: string; currency?: string };
  input_quality?: {
    price?: { latest_fetched_at?: string | null; contiguous_hours?: number };
    weather?: { latest_issued_at?: string | null; contiguous_hours?: number };
  };
  price_risk?: {
    status?: string;
    level?: "low" | "moderate" | "high" | "unknown";
    hours?: number;
    spread?: number;
    near_term_policy?: string;
    future_policy?: string;
    note?: string;
  };
}

interface PlanActionsState {
  actions: PlanAction[];
  outcome: PlanOutcome | null;
  changeSummary: PlanChangeSummary | null;
  provenance: PlanProvenance | null;
  loading: boolean;
  error: string | null;
}

const EMPTY_STATE: PlanActionsState = {
  actions: [],
  outcome: null,
  changeSummary: null,
  provenance: null,
  loading: false,
  error: null,
};

/**
 * Load actions for the selected plan, aborting an outdated request whenever
 * the selected plan changes. This keeps late responses from replacing data for
 * the plan currently displayed in the UI.
 */
export function usePlanActions(planId: number | null | undefined): PlanActionsState {
  const [state, setState] = useState<PlanActionsState>(EMPTY_STATE);

  useEffect(() => {
    if (planId == null) {
      setState(EMPTY_STATE);
      return;
    }

    const controller = new AbortController();
    setState({ actions: [], outcome: null, changeSummary: null, provenance: null, loading: true, error: null });

    fetch(`/api/plans/${planId}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`Failed to load actions (${response.status})`);
        return response.json();
      })
      .then((data) => {
        if (!controller.signal.aborted) {
          setState({
            actions: data.actions || [],
            outcome: data.outcome || null,
            changeSummary: data.change_summary || null,
            provenance: data.provenance || null,
            loading: false,
            error: null,
          });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          actions: [],
          outcome: null,
          changeSummary: null,
          provenance: null,
          loading: false,
          error: error instanceof Error ? error.message : "Failed to load plan actions",
        });
      });

    return () => controller.abort();
  }, [planId]);

  return state;
}
