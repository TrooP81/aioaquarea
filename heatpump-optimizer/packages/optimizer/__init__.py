"""Optimizer package."""

from __future__ import annotations

from typing import Any, Protocol


class Optimizer(Protocol):
    """Common interface for all optimizer layers."""

    async def generate_plan(self) -> dict[str, Any] | None:
        """Return a plan dict with horizon_start, horizon_end, actions, version, cost_estimate."""
        ...


class InfeasibleError(Exception):
    """Raised when the MILP solver finds no feasible solution."""


class DataIncompleteError(Exception):
    """Raised when required data (prices, weather) is missing for the planning horizon."""


class SolverTimeoutError(Exception):
    """Raised when the MILP solver doesn't converge within the time limit."""
