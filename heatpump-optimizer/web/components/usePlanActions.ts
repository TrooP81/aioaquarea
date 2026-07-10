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

interface PlanActionsState {
  actions: PlanAction[];
  loading: boolean;
  error: string | null;
}

const EMPTY_STATE: PlanActionsState = {
  actions: [],
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
    setState({ actions: [], loading: true, error: null });

    fetch(`/api/plans/${planId}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`Failed to load actions (${response.status})`);
        return response.json();
      })
      .then((data) => {
        if (!controller.signal.aborted) {
          setState({ actions: data.actions || [], loading: false, error: null });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          actions: [],
          loading: false,
          error: error instanceof Error ? error.message : "Failed to load plan actions",
        });
      });

    return () => controller.abort();
  }, [planId]);

  return state;
}
