from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
from aioaquarea import DataNotAvailableError

from packages.core.config import settings
from packages.core.plan_outcome import cumulative_intervals
from packages.core.resilience import ReadQuotaCategory, ReadQuotaReservation
from packages.core.services.aquarea import AquareaWrapper, ConsumptionSnapshot
from packages.poller import main as poller_main


UTC = dt.timezone.utc


class _SessionContext:
    def __init__(self) -> None:
        self.added: list[object] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def add(self, value) -> None:
        self.added.append(value)


def _record(timestamp: dt.datetime, heat_kwh: float, source_date: dt.date | None):
    return SimpleNamespace(
        ts=timestamp,
        heat_kwh=heat_kwh,
        cool_kwh=0.0,
        tank_kwh=0.0,
        source_date=source_date,
    )


def test_cumulative_intervals_use_source_date_with_utc_fallback() -> None:
    records = [
        _record(dt.datetime(2026, 7, 15, 21, 55, tzinfo=UTC), 4.0, None),
        _record(dt.datetime(2026, 7, 15, 22, 5, tzinfo=UTC), 0.2, dt.date(2026, 7, 15)),
    ]

    assert cumulative_intervals(records, "Europe/Stockholm") == []


def test_cumulative_intervals_accepts_reset_on_new_source_date() -> None:
    records = [
        _record(dt.datetime(2026, 7, 15, 21, 55, tzinfo=UTC), 4.0, dt.date(2026, 7, 15)),
        _record(dt.datetime(2026, 7, 15, 22, 5, tzinfo=UTC), 0.2, dt.date(2026, 7, 16)),
    ]

    assert cumulative_intervals(records) == [(dt.datetime(2026, 7, 15, 22, 5, tzinfo=UTC), 0.2)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("instant", "expected_date", "expected_month"),
    [
        (
            dt.datetime(2026, 7, 15, 22, 30, tzinfo=UTC),
            dt.date(2026, 7, 16),
            "20260701",
        ),
        (
            dt.datetime(2026, 7, 31, 22, 30, tzinfo=UTC),
            dt.date(2026, 8, 1),
            "20260801",
        ),
        (
            dt.datetime(2026, 1, 15, 23, 30, tzinfo=UTC),
            dt.date(2026, 1, 16),
            "20260101",
        ),
        (
            dt.datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
            dt.date(2026, 3, 29),
            "20260301",
        ),
        (
            dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
            dt.date(2026, 10, 25),
            "20261001",
        ),
    ],
)
async def test_wrapper_selects_configured_local_date_from_single_monthly_read(
    instant, expected_date, expected_month
) -> None:
    wrapper = AquareaWrapper()
    wrapper._timezone = ZoneInfo("Europe/Stockholm")
    wrapper._client = SimpleNamespace(
        get_device_consumption=AsyncMock(
            return_value=[
                SimpleNamespace(
                    data_time=(expected_date - dt.timedelta(days=1)).strftime("%Y%m%d"),
                    heat_consumption=9.0,
                    cool_consumption=9.0,
                    tank_consumption=9.0,
                ),
                SimpleNamespace(
                    data_time=expected_date.strftime("%Y%m%d"),
                    heat_consumption=1.0,
                    cool_consumption=None,
                    tank_consumption=0.0,
                ),
            ]
        )
    )
    wrapper.get_device = AsyncMock(return_value=SimpleNamespace(long_id="device-1"))
    reservation = ReadQuotaReservation("account", ReadQuotaCategory.CONSUMPTION, 1, "test")
    wrapper._assert_reservation_account = MagicMock()

    snapshot = await wrapper._fetch_consumption_unreserved(instant, reservation)

    assert snapshot.date == expected_date
    assert (snapshot.heat_kwh, snapshot.cool_kwh, snapshot.tank_kwh) == (1.0, None, 0.0)
    wrapper._client.get_device_consumption.assert_awaited_once()
    assert wrapper._client.get_device_consumption.await_args.args[2] == expected_month


@pytest.mark.asyncio
async def test_missing_exact_source_day_creates_no_consumption_row(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    sessions: list[_SessionContext] = []

    def get_session():
        session = _SessionContext()
        sessions.append(session)
        return session

    wrapper = SimpleNamespace(
        get_device=AsyncMock(
            return_value=SimpleNamespace(long_id="device-1", temperature_outdoor=4.0)
        ),
        refresh_consumption=AsyncMock(
            side_effect=DataNotAvailableError("exact source day unavailable")
        ),
    )
    monkeypatch.setattr(poller_main, "get_session", get_session)
    monkeypatch.setattr(poller_main, "get_user_tz", AsyncMock(return_value="Europe/Stockholm"))
    monkeypatch.setattr(
        poller_main,
        "resolve_outdoor_temperature",
        AsyncMock(
            return_value=SimpleNamespace(effective_c=4.0, heat_pump_c=4.0, source="heat-pump")
        ),
    )

    await poller_main.poll_consumption(wrapper)

    assert wrapper.refresh_consumption.await_count == 1
    assert all(not session.added for session in sessions)


@pytest.mark.asyncio
async def test_missing_exact_source_day_log_contains_provenance_only() -> None:
    wrapper = AquareaWrapper()
    wrapper._timezone = ZoneInfo("Europe/Stockholm")
    wrapper._client = SimpleNamespace(
        get_device_consumption=AsyncMock(
            return_value=[
                SimpleNamespace(
                    data_time="20260715",
                    heat_consumption=9.0,
                    cool_consumption=9.0,
                    tank_consumption=9.0,
                )
            ]
        )
    )
    wrapper.get_device = AsyncMock(return_value=SimpleNamespace(long_id="device-1"))
    reservation = ReadQuotaReservation("account", ReadQuotaCategory.CONSUMPTION, 1, "test")
    wrapper._assert_reservation_account = MagicMock()

    with pytest.raises(DataNotAvailableError):
        with MagicMock() as warning:
            with pytest.MonkeyPatch.context() as patches:
                patches.setattr("packages.core.services.aquarea.logger.warning", warning)
                await wrapper._fetch_consumption_unreserved(
                    dt.datetime(2026, 7, 15, 22, 30, tzinfo=UTC), reservation
                )

    warning.assert_called_once_with(
        "consumption_source_day_unavailable",
        extra={
            "poll_instant": "2026-07-15T22:30:00+00:00",
            "timezone": "Europe/Stockholm",
            "source_date": "2026-07-16",
            "category": "exact_day_missing",
        },
    )
    assert "password" not in repr(warning.call_args).lower()
    assert "response" not in repr(warning.call_args).lower()


@pytest.mark.asyncio
async def test_invalid_wrapper_timezone_allocates_no_resources(monkeypatch) -> None:
    session_factory = MagicMock()
    redis_factory = MagicMock()
    monkeypatch.setattr(
        "packages.core.settings_service.get_user_tz", AsyncMock(return_value="bad/tz")
    )
    monkeypatch.setattr("packages.core.services.aquarea.aiohttp.ClientSession", session_factory)
    monkeypatch.setattr("packages.core.services.aquarea.redis.from_url", redis_factory)

    with pytest.raises(ZoneInfoNotFoundError):
        await AquareaWrapper().start()

    session_factory.assert_not_called()
    redis_factory.assert_not_called()


@pytest.mark.asyncio
async def test_scheduled_legacy_ingestion_persists_local_source_date_and_null(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", False)
    sessions: list[_SessionContext] = []

    def get_session():
        session = _SessionContext()
        sessions.append(session)
        return session

    device = SimpleNamespace(
        long_id="device-1",
        temperature_outdoor=4.0,
        get_and_refresh_consumption=AsyncMock(side_effect=[None, 0.0, 3.0]),
    )
    monkeypatch.setattr(poller_main, "get_session", get_session)
    monkeypatch.setattr(poller_main, "get_user_tz", AsyncMock(return_value="Europe/Stockholm"))
    monkeypatch.setattr(
        poller_main,
        "resolve_outdoor_temperature",
        AsyncMock(
            return_value=SimpleNamespace(effective_c=4.0, heat_pump_c=4.0, source="heat-pump")
        ),
    )

    await poller_main.poll_consumption(SimpleNamespace(get_device=AsyncMock(return_value=device)))

    record = sessions[-1].added[0]
    assert (
        record.source_date == device.get_and_refresh_consumption.await_args_list[0].args[0].date()
    )
    assert (record.heat_kwh, record.cool_kwh, record.tank_kwh) == (None, 0.0, 3.0)
    assert device.get_and_refresh_consumption.await_count == 3


@pytest.mark.asyncio
async def test_scheduled_quota_ingestion_persists_snapshot_source_date(monkeypatch) -> None:
    monkeypatch.setattr(settings, "panasonic_distributed_read_quota_enabled", True)
    sessions: list[_SessionContext] = []

    def get_session():
        session = _SessionContext()
        sessions.append(session)
        return session

    snapshot = ConsumptionSnapshot(
        date=dt.date(2026, 7, 16),
        heat_kwh=None,
        cool_kwh=0.0,
        tank_kwh=3.0,
        fetched_at=dt.datetime.now(UTC),
    )
    wrapper = SimpleNamespace(
        get_device=AsyncMock(
            return_value=SimpleNamespace(long_id="device-1", temperature_outdoor=4.0)
        ),
        refresh_consumption=AsyncMock(return_value=snapshot),
    )
    monkeypatch.setattr(poller_main, "get_session", get_session)
    monkeypatch.setattr(poller_main, "get_user_tz", AsyncMock(return_value="Europe/Stockholm"))
    monkeypatch.setattr(
        poller_main,
        "resolve_outdoor_temperature",
        AsyncMock(
            return_value=SimpleNamespace(effective_c=4.0, heat_pump_c=4.0, source="heat-pump")
        ),
    )

    await poller_main.poll_consumption(wrapper)

    record = sessions[-1].added[0]
    assert (record.source_date, record.heat_kwh, record.cool_kwh, record.tank_kwh) == (
        dt.date(2026, 7, 16),
        None,
        0.0,
        3.0,
    )
    assert wrapper.refresh_consumption.await_count == 1
