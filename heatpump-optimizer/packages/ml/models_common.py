"""Shared ML model utilities and configuration."""

from __future__ import annotations

import datetime as dt
import os
import statistics
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import structlog

from packages.core.config import settings as app_settings

_logger = structlog.get_logger()

# Bounds (in hours) for a plausible interval between two cumulative consumption
# readings. Anything shorter is likely a duplicate poll; anything longer means we
# missed samples and the average rate over the gap would be unreliable.
MIN_INTERVAL_HOURS = 0.05  # 3 minutes
MAX_INTERVAL_HOURS = 2.0
COUNTER_CONFIRMATION_EPSILON = 0.001
COUNTER_CONFIRMATION_DIAGNOSTIC_KEYS = (
    "positive_pending",
    "positive_confirmed",
    "positive_replaced",
    "positive_abandoned",
    "positive_reverted",
    "positive_unresolved",
    "reset_pending",
    "reset_confirmed",
    "reset_replaced",
    "reset_abandoned",
    "reset_unresolved",
)


class _ConsumptionRowLike(Protocol):
    device_id: str
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


@dataclass(frozen=True)
class CounterInterval:
    """A positive change in one per-device day-to-date cumulative counter."""

    device_id: str
    start_ts: dt.datetime
    end_ts: dt.datetime
    elapsed_hours: float
    energy_kwh: float
    row: _ConsumptionRowLike


def _aware_utc(ts: dt.datetime) -> dt.datetime:
    """Normalize database instants so elapsed time is never wall-clock based."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc)


def iter_counter_change_intervals(
    rows: Iterable[_ConsumptionRowLike],
    counter: str,
    timezone: ZoneInfo,
    *,
    min_interval_hours: float = MIN_INTERVAL_HOURS,
    max_interval_hours: float = MAX_INTERVAL_HOURS,
    diagnostics: dict[str, int] | None = None,
    confirm_changes: bool = False,
) -> Iterator[CounterInterval]:
    """Yield valid windows between positive changes in a cumulative counter.

    Anchors are independent for every device and counter. Historical records
    use the UTC request date to select a daily API entry, so a UTC date change
    is a source-day reset even when it is not local midnight. Locally selected
    records reset at local midnight. A same-counter-day decrease is first
    treated as a transient correction. A second lower reading confirms a
    revised baseline and safely re-anchors subsequent intervals.
    """

    def increment(name: str) -> None:
        if diagnostics is not None:
            diagnostics[name] = diagnostics.get(name, 0) + 1

    def reset_type(anchor: _ConsumptionRowLike, row: _ConsumptionRowLike) -> str | None:
        anchor_source_date = getattr(anchor, "source_date", None)
        row_source_date = getattr(row, "source_date", None)
        if anchor_source_date is not None and row_source_date is not None:
            return "source_day_reset" if anchor_source_date != row_source_date else None
        if anchor.ts.date() != row.ts.date():
            return "source_day_reset"
        if anchor.ts.astimezone(timezone).date() != row.ts.astimezone(timezone).date():
            return "local_day_reset"
        return None

    def reset_boundary(row: _ConsumptionRowLike, kind: str) -> dt.datetime:
        end_ts = _aware_utc(row.ts)
        if kind == "source_day_reset":
            return end_ts.replace(hour=0, minute=0, second=0, microsecond=0)
        local_end = row.ts.astimezone(timezone)
        return local_end.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
            dt.timezone.utc
        )

    def interval(
        anchor: _ConsumptionRowLike,
        row: _ConsumptionRowLike,
        start_ts: dt.datetime,
        energy_kwh: float,
    ) -> CounterInterval | None:
        end_ts = _aware_utc(row.ts)
        elapsed_hours = (end_ts - start_ts).total_seconds() / 3600.0
        if not min_interval_hours <= elapsed_hours <= max_interval_hours:
            increment("outside_window")
            return None
        return CounterInterval(anchor.device_id, start_ts, end_ts, elapsed_hours, energy_kwh, row)

    if confirm_changes:
        anchors: dict[str, _ConsumptionRowLike] = {}
        pending_positive: dict[str, _ConsumptionRowLike] = {}
        pending_reset: dict[str, tuple[_ConsumptionRowLike, dt.datetime, str]] = {}
        pending_corrections: dict[str, _ConsumptionRowLike] = {}

        def begin_positive(
            device_id: str, row: _ConsumptionRowLike, *, replacement: bool = False
        ) -> None:
            pending_positive[device_id] = row
            increment("positive_replaced" if replacement else "positive_pending")

        def begin_reset(device_id: str, row: _ConsumptionRowLike, kind: str) -> None:
            pending_reset[device_id] = (row, reset_boundary(row, kind), kind)
            increment("reset_pending")
            increment(kind)

        for row in rows:
            device_id = row.device_id
            anchor = anchors.get(device_id)
            if anchor is None:
                anchors[device_id] = row
                continue

            old_value = getattr(anchor, counter) or 0.0
            new_value = getattr(row, counter) or 0.0
            pending = pending_positive.get(device_id)
            reset = pending_reset.get(device_id)

            if reset is not None:
                reset_row, boundary, reset_kind = reset
                newer_reset_kind = reset_type(reset_row, row)
                if newer_reset_kind is not None:
                    increment("reset_abandoned")
                    pending_reset.pop(device_id, None)
                    begin_reset(device_id, row, newer_reset_kind)
                    continue
                reset_value = getattr(reset_row, counter) or 0.0
                if (
                    reset_value - COUNTER_CONFIRMATION_EPSILON
                    <= new_value
                    < old_value - COUNTER_CONFIRMATION_EPSILON
                ):
                    pending_reset.pop(device_id, None)
                    increment("reset_confirmed")
                    emitted = interval(anchor, reset_row, boundary, reset_value)
                    if emitted is not None:
                        yield emitted
                    anchors[device_id] = reset_row
                    pending_corrections.pop(device_id, None)
                    if new_value > reset_value + COUNTER_CONFIRMATION_EPSILON:
                        begin_positive(device_id, row)
                    continue
                if new_value < reset_value - COUNTER_CONFIRMATION_EPSILON:
                    pending_reset[device_id] = (row, boundary, reset_kind)
                    increment("reset_replaced")
                    continue
                if new_value >= old_value - COUNTER_CONFIRMATION_EPSILON:
                    pending_reset.pop(device_id, None)
                    increment("reset_abandoned")
                    if new_value > old_value + COUNTER_CONFIRMATION_EPSILON:
                        begin_positive(device_id, row)
                    continue
                continue

            kind = reset_type(anchor, row)
            pending_reset_kind = reset_type(pending, row) if pending is not None else None
            if pending_reset_kind is not None:
                pending_positive.pop(device_id, None)
                increment("positive_abandoned")
                begin_reset(device_id, row, pending_reset_kind)
                continue

            if pending is not None:
                candidate_value = getattr(pending, counter) or 0.0
                if abs(new_value - old_value) <= COUNTER_CONFIRMATION_EPSILON:
                    pending_positive.pop(device_id, None)
                    increment("positive_reverted")
                    continue
                if (
                    old_value + COUNTER_CONFIRMATION_EPSILON
                    < new_value
                    < candidate_value - COUNTER_CONFIRMATION_EPSILON
                ):
                    begin_positive(device_id, row, replacement=True)
                    continue
                if new_value >= candidate_value - COUNTER_CONFIRMATION_EPSILON:
                    pending_positive.pop(device_id, None)
                    increment("positive_confirmed")
                    emitted = interval(
                        anchor,
                        pending,
                        _aware_utc(anchor.ts),
                        candidate_value - old_value,
                    )
                    if emitted is not None:
                        yield emitted
                    anchors[device_id] = pending
                    pending_corrections.pop(device_id, None)
                    if new_value > candidate_value + COUNTER_CONFIRMATION_EPSILON:
                        begin_positive(device_id, row)
                    continue
                if new_value < old_value - COUNTER_CONFIRMATION_EPSILON:
                    pending_positive.pop(device_id, None)
                    increment("positive_abandoned")
                else:
                    continue

            if abs(new_value - old_value) <= COUNTER_CONFIRMATION_EPSILON:
                increment("zero_delta")
                continue
            if new_value > old_value:
                pending_corrections.pop(device_id, None)
                begin_positive(device_id, row)
                continue
            if kind is not None:
                begin_reset(device_id, row, kind)
                continue
            if device_id in pending_corrections:
                anchors[device_id] = row
                pending_corrections.pop(device_id, None)
                increment("correction_reanchor")
                continue
            pending_corrections[device_id] = row
            increment("midday_decrease")

        for device_id in pending_positive:
            increment("positive_unresolved")
        for device_id in pending_reset:
            increment("reset_unresolved")
        return

    anchors: dict[str, _ConsumptionRowLike] = {}
    pending_corrections: dict[str, _ConsumptionRowLike] = {}
    for row in rows:
        device_id = row.device_id
        anchor = anchors.get(device_id)
        if anchor is None:
            anchors[device_id] = row
            continue

        old_value = getattr(anchor, counter) or 0.0
        new_value = getattr(row, counter) or 0.0
        end_ts = _aware_utc(row.ts)
        start_ts = _aware_utc(anchor.ts)
        if new_value == old_value:
            pending_corrections.pop(device_id, None)
            if diagnostics is not None:
                diagnostics["zero_delta"] = diagnostics.get("zero_delta", 0) + 1
            continue

        if new_value < old_value:
            kind = reset_type(anchor, row)
            if kind is None:
                if device_id in pending_corrections:
                    anchors[device_id] = row
                    pending_corrections.pop(device_id, None)
                    if diagnostics is not None:
                        diagnostics["correction_reanchor"] = (
                            diagnostics.get("correction_reanchor", 0) + 1
                        )
                    continue
                pending_corrections[device_id] = row
                if diagnostics is not None:
                    diagnostics["midday_decrease"] = diagnostics.get("midday_decrease", 0) + 1
                continue
            start_ts = reset_boundary(row, kind)
            energy_kwh = new_value
            pending_corrections.pop(device_id, None)
            if diagnostics is not None:
                diagnostics[kind] = diagnostics.get(kind, 0) + 1
        else:
            energy_kwh = new_value - old_value
            pending_corrections.pop(device_id, None)

        elapsed_hours = (end_ts - start_ts).total_seconds() / 3600.0
        anchors[device_id] = row
        if not min_interval_hours <= elapsed_hours <= max_interval_hours:
            if diagnostics is not None:
                diagnostics["outside_window"] = diagnostics.get("outside_window", 0) + 1
            continue
        yield CounterInterval(device_id, start_ts, end_ts, elapsed_hours, energy_kwh, row)


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

# How many timestamped checkpoints to keep per model type. Training writes a new
# ``<name>_<timestamp>.pkl`` each run; ``load_latest`` only ever reads the newest,
# so older files are dead weight. Keep a small history for rollback/debugging and
# prune the rest to stop MODEL_DIR growing without bound.
MODEL_RETENTION = 5


def prune_old_models(
    pattern: str, keep: int = MODEL_RETENTION, model_dir: Path | None = None
) -> int:
    """Delete all but the newest ``keep`` checkpoint files matching ``pattern``.

    Files are named ``<name>_YYYYmmdd_HHMM[SS].pkl`` so lexical sort is also
    chronological. Returns the number of files removed. Failures to unlink an
    individual file are logged and skipped (never raised) so a stray lock can't
    break a training run.
    """
    if keep < 0:
        raise ValueError("keep must be >= 0")

    directory = model_dir if model_dir is not None else MODEL_DIR
    files = sorted(directory.glob(pattern))
    stale = files[:-keep] if keep else list(files)

    removed = 0
    for path in stale:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            _logger.warning("model_prune_failed", path=str(path), error=str(exc))

    if removed:
        _logger.info("models_pruned", pattern=pattern, removed=removed, kept=keep)
    return removed


def time_series_cv_mae(model, X, y, max_splits: int = 5) -> tuple[float, float]:
    """Cross-validated mean absolute error with forward-chaining time-series CV.

    Returns a ``(mean_MAE, std_MAE)`` tuple where ``mean_MAE`` is the average
    per-fold MAE and ``std_MAE`` is the standard deviation of those per-fold
    MAEs (a measure of how stable the error is across folds).

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

    mean_mae = statistics.fmean(maes)
    std_mae = statistics.pstdev(maes) if len(maes) > 1 else 0.0
    return float(mean_mae), float(std_mae)


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
    path = _baseline_path(name)
    temporary_path: str | None = None
    try:
        file_descriptor, temporary_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(json.dumps(payload))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        if os.name == "posix":
            try:
                directory_descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except OSError:
                pass
    except OSError:
        _logger.warning("mae_baseline_write_failed", model=name)
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


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
