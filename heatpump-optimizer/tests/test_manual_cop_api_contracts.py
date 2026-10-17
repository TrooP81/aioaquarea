"""API contracts for the local-calendar manual controller-COP ledger (migration 036)."""

from __future__ import annotations

import datetime as dt
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.dialects import postgresql
from structlog.testing import capture_logs

UTC = dt.timezone.utc


def _session_context(session):
    class _AsyncContextManager:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            return False

    return _AsyncContextManager()


def _result(row=None, rows=None, scalar=None):
    return SimpleNamespace(
        scalar=lambda: scalar,
        scalar_one_or_none=lambda: row,
        scalars=lambda: SimpleNamespace(all=lambda: list(rows or [])),
    )


def _stored(**changes):
    now = dt.datetime.now(UTC).replace(microsecond=0)
    row = {
        "device_id": "fixture-device",
        "granularity": "day",
        "period_date": now.date(),
        "timezone": "UTC",
        "value": 9.7,
        "observed_at": now,
        "completeness": "partial",
        "created_at": now,
        "updated_at": now,
        "revision": 9007199254740993,
    }
    return SimpleNamespace(**(row | changes))


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


class TestManualCOPLocalCalendarAPI:
    @pytest.fixture
    async def client(self):
        import httpx

        from packages.api.main import app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {os.environ['API_TOKEN']}"},
        ) as client:
            yield client

    @staticmethod
    def body(row, revision="0"):
        return {
            "value": row.value,
            "observed_at": row.observed_at.isoformat(),
            "completeness": row.completeness,
            "timezone": row.timezone,
            "expected_revision": revision,
        }

    async def test_requests_without_bearer_token_are_refused(self, client):
        sessions = AsyncMock(side_effect=AssertionError("no storage access"))
        from packages.api.routers import feeds

        with patch.object(feeds, "get_session", sessions):
            response = await client.get(
                "/api/cop/manual", headers={"Authorization": "Bearer wrong-token"}
            )
            missing = await client.get("/api/cop/manual", headers={"Authorization": ""})
        assert response.status_code == 403
        assert missing.status_code == 401
        sessions.assert_not_called()

    @pytest.mark.parametrize(
        ("granularity", "period_date", "timezone", "start", "end"),
        [
            (
                "day",
                dt.date(2026, 3, 29),
                "Europe/Stockholm",
                dt.datetime(2026, 3, 28, 23, tzinfo=UTC),
                dt.datetime(2026, 3, 29, 22, tzinfo=UTC),
            ),
            (
                "day",
                dt.date(2026, 10, 25),
                "Europe/Stockholm",
                dt.datetime(2026, 10, 24, 22, tzinfo=UTC),
                dt.datetime(2026, 10, 25, 23, tzinfo=UTC),
            ),
            (
                "month",
                dt.date(2026, 9, 1),
                "America/Havana",
                dt.datetime(2026, 9, 1, 4, tzinfo=UTC),
                dt.datetime(2026, 10, 1, 4, tzinfo=UTC),
            ),
        ],
    )
    async def test_put_stores_local_identity_only_and_returns_derived_boundaries(
        self, client, monkeypatch, granularity, period_date, timezone, start, end
    ):
        from packages.api.routers import feeds

        class FixedDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt.datetime(2026, 11, 3, tzinfo=UTC).astimezone(tz)

        monkeypatch.setattr(
            feeds, "dt", SimpleNamespace(datetime=FixedDatetime, timezone=dt.timezone)
        )
        row = _stored(
            granularity=granularity,
            period_date=period_date,
            timezone=timezone,
            observed_at=end + dt.timedelta(hours=2),
            completeness="closed",
            revision=1,
        )
        session = SimpleNamespace(
            execute=AsyncMock(side_effect=[_result(scalar=True), _result(row=row)])
        )
        with patch.object(feeds, "get_session", return_value=_session_context(session)):
            response = await client.put(
                f"/api/cop/manual/fixture-device/{granularity}/{period_date}",
                json=self.body(row),
            )
        assert response.status_code == 201
        payload = response.json()
        assert dt.datetime.fromisoformat(payload["period_start"]) == start
        assert dt.datetime.fromisoformat(payload["period_end"]) == end
        assert payload["scope"] == "unknown"
        assert payload["measured"] is False
        assert payload["revision"] == "1"
        statement = session.execute.call_args_list[1].args[0]
        parameters = statement.compile(dialect=postgresql.dialect()).params
        assert {"period_start", "period_end"}.isdisjoint(parameters)
        assert parameters["timezone"] == timezone
        assert parameters["period_date"] == period_date
        assert "period_start" not in _sql(statement)
        assert "period_end" not in _sql(statement)

    async def test_put_rejects_invalid_returned_row_without_success(self, client):
        from packages.api.routers import feeds

        row = _stored(completeness="closed")
        returned = _stored(completeness="closed", observed_at=row.observed_at, timezone="Bad/Zone")
        session = SimpleNamespace(
            execute=AsyncMock(side_effect=[_result(scalar=True), _result(row=returned)])
        )
        row.period_date = row.observed_at.date() - dt.timedelta(days=1)
        with (
            patch.object(feeds, "get_session", return_value=_session_context(session)),
            capture_logs() as logs,
        ):
            response = await client.put(
                f"/api/cop/manual/fixture-device/day/{row.period_date}",
                json=self.body(row),
            )
        assert response.status_code == 503
        assert response.json() == {"detail": "Manual COP stored period is invalid"}
        assert any(entry.get("outcome") == "invalid_stored_period" for entry in logs)

    async def test_page_with_invalid_stored_period_is_unavailable_not_reconstructed(self, client):
        from packages.api.routers import feeds

        valid = _stored()
        invalid = _stored(
            period_date=valid.period_date - dt.timedelta(days=3), timezone="Mars/Olympus"
        )
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    _result(),
                    _result(rows=["fixture-device"]),
                    _result(rows=[valid, invalid]),
                ]
            )
        )
        with (
            patch.object(feeds, "get_session", return_value=_session_context(session)),
            patch.object(feeds, "get_bool_setting", new=AsyncMock(return_value=True)),
            patch.object(feeds, "get_user_tz", new=AsyncMock(return_value="UTC")),
            capture_logs() as logs,
        ):
            response = await client.get("/api/cop/manual?device_id=fixture-device")
        assert response.status_code == 503
        assert response.json() == {"detail": "Manual COP stored period is invalid"}
        assert "fixture-device" not in str(logs)
        assert [
            entry["outcome"]
            for entry in logs
            if entry["event"] == "manual_cop_invalid_stored_period"
        ] == ["page_refused"]

    @pytest.mark.parametrize("enabled", [True, False])
    async def test_invalid_forecast_candidate_refuses_projection_when_enabled(
        self, client, enabled
    ):
        from packages.api.routers import feeds

        today = dt.datetime.now(UTC).date()
        latest = _stored()
        invalid = _stored(
            period_date=today - dt.timedelta(days=2),
            observed_at=dt.datetime.combine(today - dt.timedelta(days=5), dt.time(), UTC),
        )
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    _result(),
                    _result(rows=["fixture-device"]),
                    _result(rows=[]),
                    _result(row=latest),
                    _result(rows=[invalid]),
                    _result(row=None),
                ]
            )
        )
        forecast = AsyncMock(wraps=feeds.forecast_snapshot)
        with (
            patch.object(feeds, "get_session", return_value=_session_context(session)),
            patch.object(feeds, "get_bool_setting", new=AsyncMock(return_value=enabled)),
            patch.object(feeds, "get_user_tz", new=AsyncMock(return_value="UTC")),
            patch.object(feeds, "forecast_snapshot", new=forecast),
        ):
            response = await client.get("/api/cop/manual?device_id=fixture-device")
        assert response.status_code == 200
        monitoring = response.json()["monitoring"]
        if enabled:
            assert monitoring["status"] == "unavailable"
            assert monitoring["reason"] == "invalid_stored_period"
            assert monitoring["forecast"] is None
            forecast.assert_not_awaited()
        else:
            forecast.assert_awaited_once()
            assert forecast.await_args.args[0] == ()
            assert forecast.await_args.args[2] is False

    async def test_latest_closed_candidate_query_has_no_stored_boundary_filter(self, client):
        from packages.api.routers import feeds

        today = dt.datetime.now(UTC).date()
        latest = _stored()
        closed = _stored(
            period_date=today - dt.timedelta(days=40),
            observed_at=dt.datetime.combine(today - dt.timedelta(days=39), dt.time(), UTC),
            completeness="closed",
        )
        session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    _result(),
                    _result(rows=["fixture-device"]),
                    _result(rows=[latest]),
                    _result(row=latest),
                    _result(rows=[]),
                    _result(row=closed),
                ]
            )
        )
        forecast = AsyncMock(wraps=feeds.forecast_snapshot)
        with (
            patch.object(feeds, "get_session", return_value=_session_context(session)),
            patch.object(feeds, "get_bool_setting", new=AsyncMock(return_value=True)),
            patch.object(feeds, "get_user_tz", new=AsyncMock(return_value="UTC")),
            patch.object(feeds, "forecast_snapshot", new=forecast),
        ):
            response = await client.get("/api/cop/manual?device_id=fixture-device")
        assert response.status_code == 200
        observation = response.json()["observations"][0]
        assert observation["scope"] == "unknown"
        assert dt.datetime.fromisoformat(observation["period_start"]) == dt.datetime.combine(
            today, dt.time(), UTC
        )
        statements = [_sql(call.args[0]) for call in session.execute.call_args_list[1:]]
        assert all("period_start" not in sql and "period_end" not in sql for sql in statements)
        assert "manual_cop_observations.completeness = " in statements[-1]
        candidates = forecast.await_args.args[0]
        assert [item.period_date for item in candidates] == [latest.period_date, closed.period_date]
        assert candidates[1].period_end <= candidates[1].observed_at

    def test_response_schema_scope_is_unknown_and_not_client_settable(self):
        from pydantic import ValidationError

        from dataclasses import asdict

        from packages.api.schemas import ManualCOPObservationResponse, ManualCOPPut
        from packages.core.cop_monitoring import snapshot_from_stored

        response = ManualCOPObservationResponse(**asdict(snapshot_from_stored(_stored())))
        assert response.scope == "unknown"
        assert ManualCOPObservationResponse.model_fields["scope"].default == "unknown"
        with pytest.raises(ValidationError):
            ManualCOPObservationResponse(**(response.model_dump() | {"scope": "total"}))
        with pytest.raises(ValidationError):
            ManualCOPPut(
                value=9.7,
                observed_at="2026-10-04T08:00:00Z",
                completeness="partial",
                timezone="UTC",
                expected_revision="0",
                period_start="2026-10-04T00:00:00Z",
            )
