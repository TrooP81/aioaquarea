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

    @property
    def heat_rate_kw(self) -> float:
        """Average *space-heating* electrical demand over the interval (kW).

        Space heating is the only load whose physics match the demand model's
        monotonic constraints (demand falls as it warms up outside). DHW and
        cooling are deliberately excluded — DHW is outdoor-independent and
        cooling has the opposite temperature relationship.
        """
        return self.heat_kwh / self.elapsed_hours if self.elapsed_hours > 0 else 0.0

    @property
    def cool_rate_kw(self) -> float:
        """Average *cooling* electrical demand over the interval (kW)."""
        return self.cool_kwh / self.elapsed_hours if self.elapsed_hours > 0 else 0.0


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
    from sklearn.ensemble import (
        GradientBoostingRegressor,
        HistGradientBoostingRegressor,
    )
    from sklearn.model_selection import TimeSeriesSplit, cross_val_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False
    GradientBoostingRegressor = None
    HistGradientBoostingRegressor = None
    cross_val_score = None
    TimeSeriesSplit = None
    Pipeline = None
    StandardScaler = None

MODEL_DIR = Path(app_settings.model_dir)
MODEL_DIR.mkdir(parents=True, exist_ok=True)


def time_series_cv_mae(model, X, y, max_splits: int = 5) -> tuple[float, float]:
    """Return (mean_MAE, std_MAE) using forward-chaining time-series CV.

    ``X``/``y`` **must** already be ordered chronologically. Unlike plain
    ``KFold`` (which shuffles future rows into the training folds and leaks
    information backwards in time), ``TimeSeriesSplit`` always trains on the
    past and validates on the immediately following slice — an honest estimate
    of how the model will perform on genuinely unseen future data.

    Falls back to a single in-sample MAE when there are too few samples to form
    at least two folds.
    """
    if not HAS_SKLEARN:
        raise ImportError("scikit-learn required: pip install scikit-learn")

    from sklearn.base import clone
    from sklearn.metrics import mean_absolute_error

    n = len(X)
    n_splits = min(max_splits, n - 1)
    if n_splits < 2:
        model.fit(X, y)
        return float(mean_absolute_error(y, model.predict(X))), 0.0

    splitter = TimeSeriesSplit(n_splits=n_splits)
    maes: list[float] = []
    for train_idx, test_idx in splitter.split(X):
        fold = clone(model)
        fold.fit(X[train_idx], y[train_idx])
        maes.append(mean_absolute_error(y[test_idx], fold.predict(X[test_idx])))

    import numpy as _np

    arr = _np.array(maes)
    return float(arr.mean()), float(arr.std())


def make_monotonic_regressor(monotonic_cst, **overrides):
    """Build a gradient-boosting regressor with physical monotonicity constraints.

    ``monotonic_cst`` is a per-feature list of ``+1`` (output must be
    non-decreasing in the feature), ``-1`` (non-increasing), or ``0`` (no
    constraint). Enforcing these relationships keeps predictions physically
    sensible — e.g. more heat input can never lower the predicted indoor
    temperature — even when the training data is noisy or sparse.

    ``HistGradientBoostingRegressor`` is used because it natively supports
    monotonic constraints and needs no feature scaling (it is tree-based), so
    the previous ``StandardScaler`` pipeline is unnecessary.
    """
    if not HAS_SKLEARN:
        raise ImportError("scikit-learn required: pip install scikit-learn")
    params = dict(
        max_iter=300,
        max_depth=4,
        learning_rate=0.05,
        l2_regularization=1.0,
        random_state=42,
    )
    params.update(overrides)
    return HistGradientBoostingRegressor(monotonic_cst=list(monotonic_cst), **params)


# Fractional MAE increase tolerated before a retrained model is treated as a
# regression and withheld from deployment (keeps the previously good model live).
MAE_REGRESSION_TOLERANCE = 0.10


def _baseline_path(name: str) -> Path:
    return MODEL_DIR / f"{name}_mae_baseline.json"


def read_mae_baseline(name: str) -> float | None:
    """Return the last-deployed MAE for model ``name``, or ``None`` if unknown."""
    import json

    path = _baseline_path(name)
    if not path.exists():
        return None
    try:
        return float(json.loads(path.read_text()).get("mae"))
    except (ValueError, OSError, TypeError):
        return None


def write_mae_baseline(name: str, mae: float) -> None:
    """Persist the MAE of the newly deployed model ``name`` as the new baseline."""
    import json

    payload = {"mae": float(mae), "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    try:
        _baseline_path(name).write_text(json.dumps(payload))
    except OSError:
        _logger.warning("mae_baseline_write_failed", model=name)


def evaluate_regression(name: str, mae: float, has_prior_model: bool) -> dict[str, object]:
    """Decide whether a freshly trained model should be deployed.

    Compares ``mae`` against the persisted baseline for ``name``. Deployment is
    withheld only when a usable prior model already exists *and* the new MAE is
    worse than the baseline by more than :data:`MAE_REGRESSION_TOLERANCE` — so a
    noisy retrain can never replace a better model, while first-ever training
    always deploys.

    Returns a dict with ``deploy`` (bool), ``baseline_mae`` and ``improved``.
    """
    baseline = read_mae_baseline(name)
    if baseline is None or not has_prior_model:
        return {"deploy": True, "baseline_mae": baseline, "improved": True}
    regressed = mae > baseline * (1.0 + MAE_REGRESSION_TOLERANCE)
    if regressed:
        _logger.warning(
            "model_retrain_regressed",
            model=name,
            new_mae=round(mae, 4),
            baseline_mae=round(baseline, 4),
            tolerance=MAE_REGRESSION_TOLERANCE,
        )
    return {"deploy": not regressed, "baseline_mae": baseline, "improved": mae <= baseline}
