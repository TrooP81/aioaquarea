"""Shared ML model utilities and configuration."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog

from packages.core.config import settings as app_settings

_logger = structlog.get_logger()

# Bounds (in hours) for a plausible interval between two cumulative consumption
# readings. Anything shorter is likely a duplicate poll; anything longer means we
# missed samples and the average rate over the gap would be unreliable.
MIN_INTERVAL_HOURS = 0.05  # 3 minutes
MAX_INTERVAL_HOURS = 2.0


class _ConsumptionRowLike(Protocol):
    ts: dt.datetime
    heat_kwh: float | None
    cool_kwh: float | None
    tank_kwh: float | None
    outdoor_temp: float | None


@dataclass(frozen=True)
class ConsumptionInterval:
    """Energy actually used during one interval, recovered from cumulative counters.

    The Panasonic API reports day-to-date cumulative kWh that resets at midnight,
    so ``heat_kwh``/``cool_kwh``/``tank_kwh`` here are *deltas* over the interval
    ending at ``ts`` (never the raw cumulative readings).
    """

    ts: dt.datetime
    elapsed_hours: float
    heat_kwh: float
    cool_kwh: float
    tank_kwh: float
    outdoor_temp: float | None

    @property
    def total_kwh(self) -> float:
        return self.heat_kwh + self.cool_kwh + self.tank_kwh

    @property
    def total_rate_kw(self) -> float:
        """Average electrical demand over the interval, in kW (kWh per hour)."""
        return self.total_kwh / self.elapsed_hours if self.elapsed_hours > 0 else 0.0


def iter_consumption_intervals(
    rows: Iterable[_ConsumptionRowLike],
) -> Iterator[ConsumptionInterval]:
    """Convert cumulative day-to-date consumption counters into per-interval deltas.

    ``rows`` must be ordered by ``ts``. Consecutive readings within the same
    calendar day are subtracted to recover the energy used during the interval.
    Pairs that straddle a day boundary (counter reset) or whose elapsed time falls
    outside ``[MIN_INTERVAL_HOURS, MAX_INTERVAL_HOURS]`` are skipped. Negative
    per-field deltas (mid-day resets / corrections) are clamped to zero.
    """
    prev = None
    for row in rows:
        if prev is not None and row.ts.date() == prev.ts.date():
            elapsed = (row.ts - prev.ts).total_seconds() / 3600.0
            if MIN_INTERVAL_HOURS <= elapsed <= MAX_INTERVAL_HOURS:
                yield ConsumptionInterval(
                    ts=row.ts,
                    elapsed_hours=elapsed,
                    heat_kwh=max(0.0, (row.heat_kwh or 0.0) - (prev.heat_kwh or 0.0)),
                    cool_kwh=max(0.0, (row.cool_kwh or 0.0) - (prev.cool_kwh or 0.0)),
                    tank_kwh=max(0.0, (row.tank_kwh or 0.0) - (prev.tank_kwh or 0.0)),
                    outdoor_temp=row.outdoor_temp,
                )
        prev = row

try:
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import cross_val_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False
    GradientBoostingRegressor = None
    cross_val_score = None
    Pipeline = None
    StandardScaler = None

MODEL_DIR = Path(app_settings.model_dir)
MODEL_DIR.mkdir(parents=True, exist_ok=True)
