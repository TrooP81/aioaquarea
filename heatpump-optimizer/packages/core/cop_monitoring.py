"""Manual controller-estimate monitoring, independent of planning and saved models."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones


_SUPPORTED_TIMEZONES = frozenset(
    timezone
    for timezone in available_timezones()
    if timezone not in {"localtime", "posixrules"} and not timezone.startswith(("posix/", "right/"))
)


def local_instants(local_time: dt.datetime, timezone: ZoneInfo) -> tuple[dt.datetime, ...]:
    instants = set()
    for fold in (0, 1):
        candidate = local_time.replace(tzinfo=timezone, fold=fold).astimezone(dt.timezone.utc)
        if candidate.astimezone(timezone).replace(tzinfo=None) == local_time:
            instants.add(candidate)
    return tuple(sorted(instants))


def period_bounds(
    granularity: str, period_date: dt.date, timezone: str
) -> tuple[dt.datetime, dt.datetime]:
    if granularity not in {"day", "month"}:
        raise ValueError("Granularity must be day or month")
    if granularity == "month" and period_date.day != 1:
        raise ValueError("A monthly period must start on the first day")
    if timezone not in _SUPPORTED_TIMEZONES:
        raise ValueError("Invalid timezone or calendar period")
    try:
        zone = ZoneInfo(timezone)
        if granularity == "day":
            end_date = period_date + dt.timedelta(days=1)
        elif period_date.month == 12:
            end_date = dt.date(period_date.year + 1, 1, 1)
        else:
            end_date = dt.date(period_date.year, period_date.month + 1, 1)
        boundaries = [
            local_instants(dt.datetime.combine(boundary, dt.time()), zone)
            for boundary in (period_date, end_date)
        ]
    except (ZoneInfoNotFoundError, OverflowError) as exc:
        raise ValueError("Invalid timezone or calendar period") from exc
    if any(not boundary for boundary in boundaries):
        raise ValueError("The calendar period has a nonexistent local midnight")
    return boundaries[0][0], boundaries[1][0]


def validate_observation_period(
    granularity: str,
    period_date: dt.date,
    timezone: str,
    observed_at: dt.datetime,
    completeness: str,
    now: dt.datetime,
) -> tuple[dt.datetime, dt.datetime]:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("Observation time must include a timezone")
    if completeness not in {"partial", "closed", "unknown"}:
        raise ValueError("Invalid completeness")
    start, end = period_bounds(granularity, period_date, timezone)
    observed_at = observed_at.astimezone(dt.timezone.utc)
    if observed_at < start or observed_at > now:
        raise ValueError("Observation time must be between period start and now")
    if completeness == "closed" and observed_at < end:
        raise ValueError("A closed period requires observation at or after period end")
    return start, end


@dataclass(frozen=True)
class ManualCOPObservation:
    device_id: str
    granularity: Literal["day", "month"]
    period_date: dt.date
    timezone: str
    period_start: dt.datetime
    period_end: dt.datetime
    value: float
    observed_at: dt.datetime
    completeness: Literal["partial", "closed", "unknown"]
    created_at: dt.datetime
    updated_at: dt.datetime
    revision: str

    @property
    def available_at(self) -> dt.datetime:
        return max(self.observed_at, self.created_at, self.updated_at)


class InvalidStoredPeriod(ValueError):
    """A persisted ledger row no longer forms a valid local calendar observation."""


def snapshot_from_stored(row) -> ManualCOPObservation:
    """Derive calendar boundaries for a stored row and re-check its attestation.

    Boundaries are never persisted: they come from the local period identity and
    the confirmed IANA timezone. Rows that cannot be derived or whose stored
    observation time contradicts the derived period are refused rather than
    given invented boundaries.
    """
    try:
        if row.completeness not in {"partial", "closed", "unknown"}:
            raise ValueError("completeness")
        value = float(row.value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("value")
        timestamps = (row.observed_at, row.created_at, row.updated_at)
        if any(
            not isinstance(moment, dt.datetime) or moment.utcoffset() is None
            for moment in timestamps
        ):
            raise ValueError("timestamps")
        observed_at, created_at, updated_at = (
            moment.astimezone(dt.timezone.utc) for moment in timestamps
        )
        start, end = period_bounds(row.granularity, row.period_date, row.timezone)
        if observed_at < start or (row.completeness == "closed" and observed_at < end):
            raise ValueError("attestation")
        revision = int(row.revision)
        if revision <= 0:
            raise ValueError("revision")
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise InvalidStoredPeriod("invalid_stored_period") from exc
    return ManualCOPObservation(
        device_id=row.device_id,
        granularity=row.granularity,
        period_date=row.period_date,
        timezone=row.timezone,
        period_start=start,
        period_end=end,
        value=value,
        observed_at=observed_at,
        completeness=row.completeness,
        created_at=created_at,
        updated_at=updated_at,
        revision=str(revision),
    )


def historical_issue_at(target_date: dt.date, issue_at: dt.datetime, timezone: str) -> dt.datetime:
    zone = ZoneInfo(timezone)
    live_local = issue_at.astimezone(zone)
    historical_date = target_date - dt.timedelta(days=1)
    local_time = dt.datetime.combine(historical_date, live_local.time().replace(tzinfo=None))
    instants = local_instants(local_time, zone)
    if not instants:
        raise ValueError("nonexistent_historical_issue_time")
    if len(instants) == 1:
        return instants[0]
    return local_time.replace(tzinfo=zone, fold=live_local.fold).astimezone(dt.timezone.utc)


def monitoring_result(
    observations: tuple[ManualCOPObservation, ...], issue_at: dt.datetime
) -> dict:
    daily = sorted(
        (row for row in observations if row.granularity == "day"),
        key=lambda row: (row.device_id, row.period_date),
    )
    issue_at = issue_at.astimezone(dt.timezone.utc)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "issue_at": issue_at.isoformat(),
                "daily": [
                    [
                        row.device_id,
                        row.period_date.isoformat(),
                        row.revision,
                        row.timezone,
                        row.period_start.isoformat(),
                        row.period_end.isoformat(),
                    ]
                    for row in daily
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return {
        "status": "insufficient_data",
        "reason": "requires_28_consecutive_closed_days",
        "issue_at": issue_at,
        "timezone": None,
        "local_issue_at": None,
        "target_date": None,
        "fingerprint": fingerprint,
        "counts": {
            "daily_rows": len(daily),
            "required_closed_days": 28,
            "available_closed_days": 0,
            "required_folds": 7,
            "minimum_fold_rows": 20,
            "fold_training_rows": [],
        },
        "forecast": None,
    }


def forecast_manual_cop(
    observations: tuple[ManualCOPObservation, ...], issue_at: dt.datetime, enabled: bool
) -> dict:
    result = monitoring_result(observations, issue_at)
    if not enabled:
        return result | {"status": "disabled", "reason": "forecasting_disabled"}
    daily = [row for row in observations if row.granularity == "day"]
    if not daily:
        return result | {"status": "no_data", "reason": "no_daily_observations"}
    if len({row.device_id for row in daily}) != 1:
        return result | {"reason": "mixed_devices"}
    available = [row for row in daily if row.available_at <= issue_at]
    if not available:
        return result | {"reason": "no_current_revision_available"}
    latest = max(available, key=lambda row: row.period_date)
    try:
        zone = ZoneInfo(latest.timezone)
        live_local = issue_at.astimezone(zone)
        today = live_local.date()
        tomorrow = today + dt.timedelta(days=1)
    except (ValueError, OverflowError, ZoneInfoNotFoundError):
        return result | {"status": "unavailable", "reason": "invalid_snapshot_timezone"}
    result.update(timezone=latest.timezone, local_issue_at=live_local, target_date=tomorrow)
    window_dates = [today - dt.timedelta(days=offset) for offset in range(28, 0, -1)]
    closed = {
        row.period_date: row
        for row in available
        if row.completeness == "closed"
        and row.timezone == latest.timezone
        and row.observed_at >= row.period_end
        and row.period_date < today
    }
    training = [closed[period_date] for period_date in window_dates if period_date in closed]
    result["counts"]["available_closed_days"] = len(training)
    if any(
        row.timezone != latest.timezone and row.period_date in window_dates for row in available
    ):
        return result | {"reason": "mixed_timezones"}
    if closed and max(closed) < today - dt.timedelta(days=1):
        return result | {"status": "stale", "reason": "latest_closed_day_precedes_yesterday"}
    if len(training) != 28:
        return result
    if any(not math.isfinite(row.value) or row.value <= 0 for row in training):
        return result | {"status": "unavailable", "reason": "invalid_snapshot_value"}
    folds = []
    for target_date in window_dates[-7:]:
        try:
            historical_issue = historical_issue_at(target_date, issue_at, latest.timezone)
        except ValueError as exc:
            return result | {"status": "unavailable", "reason": str(exc)}
        eligible = [
            row
            for row in training
            if row.period_date <= target_date - dt.timedelta(days=2)
            and row.available_at <= historical_issue
        ]
        folds.append((eligible, closed[target_date]))
    result["counts"]["fold_training_rows"] = [len(eligible) for eligible, _ in folds]
    if any(len(eligible) < 20 for eligible, _ in folds):
        return result | {"reason": "historical_fold_requires_20_available_current_revisions"}
    try:
        import numpy as np
        from sklearn.linear_model import LinearRegression
    except ImportError:
        return result | {"status": "unavailable", "reason": "optional_ml_dependencies_unavailable"}
    try:

        def fit(rows):
            estimator = LinearRegression()
            features = np.asarray([row.period_date.toordinal() for row in rows], dtype=float)
            values = np.asarray([row.value for row in rows], dtype=float)
            estimator.fit(features.reshape(-1, 1), np.log(values))
            return estimator

        with np.errstate(over="raise", invalid="raise", divide="raise"):
            errors = []
            for eligible, target in folds:
                predicted = float(fit(eligible).predict([[target.period_date.toordinal()]])[0])
                errors.append(abs(math.log(target.value) - predicted))
            estimator = fit(training)
            projected_log = float(estimator.predict([[tomorrow.toordinal()]])[0])
            log_trend = float(estimator.coef_[0])
            width = float(np.quantile(np.asarray(errors), 0.9, method="linear"))
            value, lower, upper = map(
                float, np.exp([projected_log, projected_log - width, projected_log + width])
            )
            trend_pct = float(np.expm1(log_trend) * 100)
        if not all(math.isfinite(number) for number in (value, lower, upper, log_trend, trend_pct)):
            raise ValueError("Nonfinite projection")
        if min(value, lower, upper) <= 0 or not math.isfinite(width):
            raise ValueError("Nonpositive projection")
    except Exception:
        return result | {"status": "unavailable", "reason": "invalid_projection"}
    return result | {
        "status": "ready",
        "reason": "matched_clock_retrospective_folds",
        "forecast": {
            "value": value,
            "lower": lower,
            "upper": upper,
            "log_daily_trend": log_trend,
            "daily_trend_pct": trend_pct,
            "band_label": "experimental empirical log-error band, uncalibrated",
        },
    }


_FIT_LIMIT = asyncio.Semaphore(2)


async def forecast_snapshot(
    observations: tuple[ManualCOPObservation, ...], issue_at: dt.datetime, enabled: bool
) -> dict:
    if not enabled:
        return forecast_manual_cop(observations, issue_at, False)
    result = monitoring_result(observations, issue_at)
    acquired = False
    try:
        await asyncio.wait_for(_FIT_LIMIT.acquire(), timeout=5)
        acquired = True
        task = asyncio.create_task(
            asyncio.to_thread(forecast_manual_cop, observations, issue_at, enabled)
        )
        task.add_done_callback(lambda completed: _FIT_LIMIT.release())
        acquired = False
        return await asyncio.wait_for(asyncio.shield(task), timeout=5)
    except TimeoutError:
        return result | {"status": "unavailable", "reason": "forecast_timeout"}
    except Exception:
        return result | {"status": "unavailable", "reason": "forecast_failed"}
    finally:
        if acquired:
            _FIT_LIMIT.release()
