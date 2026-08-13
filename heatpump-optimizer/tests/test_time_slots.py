import datetime as dt

from packages.core.time_slots import next_hour_boundary


def test_next_hour_boundary_keeps_exact_hour():
    now = dt.datetime(2026, 7, 14, 15, tzinfo=dt.timezone.utc)

    assert next_hour_boundary(now) == now


def test_next_hour_boundary_never_returns_a_started_price_hour():
    now = dt.datetime(2026, 7, 14, 15, 24, 1, tzinfo=dt.timezone.utc)

    assert next_hour_boundary(now) == dt.datetime(2026, 7, 14, 16, tzinfo=dt.timezone.utc)
