"""Real rollback expiry at the two G03 API boundaries, without a DB service.

SQLite proves ORM lifetime behavior, not PostgreSQL read-only enforcement.
Only PostgreSQL connection options and SET LOCAL are recorded by the facade.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles, deregister
from sqlalchemy.orm import Session, attributes
from sqlalchemy.orm.exc import DetachedInstanceError

from packages.core import database
from packages.core.models import (
    AuditLogRecord,
    DeviceStatusRecord,
    IndoorTempReading,
    SettingRecord,
    SpaceHeatingGateRecord,
)
from packages.core.space_heating_gate import HeatingGateConfig

UTC = dt.timezone.utc


@pytest.fixture
def orm_db(monkeypatch):
    """Keep real persistent rows alive through production rollback and close."""

    @compiles(JSONB, "sqlite")
    def sqlite_jsonb(_type, _compiler, **_kwargs):
        return "JSON"

    engine = create_engine("sqlite://")
    tables = [
        model.__table__
        for model in (
            DeviceStatusRecord,
            SpaceHeatingGateRecord,
            IndoorTempReading,
            SettingRecord,
            AuditLogRecord,
        )
    ]
    for table in tables:
        table.create(engine)

    def normalize_clocks(_session, row):
        for column in inspect(row).mapper.column_attrs:
            value = getattr(row, column.key)
            if isinstance(value, dt.datetime) and value.tzinfo is None:
                attributes.set_committed_value(row, column.key, value.replace(tzinfo=UTC))

    sessions = []
    statements = []
    injection = SimpleNamespace(error=None, select_number=2)

    def select_only(_conn, _cursor, statement, _parameters, _context, _many):
        assert statement.lstrip().upper().startswith("SELECT"), statement
        statements.append(statement)

    class AsyncFacade:
        def __init__(self, **kwargs):
            self.sync = Session(engine, expire_on_commit=False, **kwargs)
            event.listen(self.sync, "loaded_as_persistent", normalize_clocks)
            self.rows = []
            self.options = []
            self.timeouts = []
            self.rollbacks = 0
            self.commits = 0
            self.closed = False
            self.select_count = 0
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            self.sync.close()
            self.closed = True

        async def connection(self, *, execution_options):
            self.options.append(execution_options)

        async def execute(self, statement):
            if str(statement) == "SET LOCAL statement_timeout = '2s'":
                self.timeouts.append(str(statement))
                return None
            assert statement.is_select, "GET must not issue DML"
            self.select_count += 1
            if injection.error is not None and self.select_count == injection.select_number:
                raise injection.error
            result = self.sync.execute(statement)
            # SQLAlchemy's identity map is weak: retain loaded objects without
            # consuming or replacing the actual query result.
            self.rows.extend(self.sync.identity_map.values())
            return result

        async def rollback(self):
            self.rows.extend(self.sync.identity_map.values())
            self.sync.rollback()
            self.rollbacks += 1

        async def commit(self):
            self.sync.commit()
            self.commits += 1

    def seed(*rows):
        with Session(engine) as setup:
            setup.add_all(rows)
            setup.commit()

    def assert_cleanup():
        assert sessions
        for session in sessions:
            assert session.closed
            if session.options:
                assert session.options == [{"postgresql_readonly": True}]
                assert session.timeouts == ["SET LOCAL statement_timeout = '2s'"]
                assert session.sync.autoflush is False
                assert session.rollbacks == 1
                assert session.commits == 0
                for row in session.rows:
                    assert inspect(row).detached
                    assert inspect(row).expired_attributes

    monkeypatch.setattr(database, "async_session_factory", AsyncFacade)
    fixture = SimpleNamespace(
        engine=engine,
        sessions=sessions,
        statements=statements,
        seed=seed,
        assert_cleanup=assert_cleanup,
        guard=lambda: event.listen(engine, "before_cursor_execute", select_only),
        injection=injection,
    )
    try:
        yield fixture
    finally:
        if event.contains(engine, "before_cursor_execute", select_only):
            event.remove(engine, "before_cursor_execute", select_only)
        for session in sessions:
            event.remove(session.sync, "loaded_as_persistent", normalize_clocks)
            session.sync.close()
        engine.dispose()
        deregister(JSONB)


def _rows(now):
    config = HeatingGateConfig()
    return (
        DeviceStatusRecord(
            device_id="A",
            ts=now,
            outdoor_temp=5.0,
            device_action="HEATING",
            space_heating_active=True,
            space_heating_evidence="compressor_heating",
            defrost_active=False,
        ),
        SpaceHeatingGateRecord(
            device_id="A",
            state="ALLOWED",
            config_fingerprint=config.fingerprint,
            base_c=config.base_c,
            on_threshold_c=config.on_threshold_c,
            off_threshold_c=config.off_threshold_c,
            last_raw_outdoor_c=5.0,
            reason_code="test_allowed",
            source_status_ts=now,
            evaluated_at=now,
        ),
    )


async def test_g03_rollback_control_expires_and_detaches_real_rows(orm_db):
    orm_db.seed(*_rows(dt.datetime.now(UTC)))
    orm_db.guard()
    async with database.get_session(read_only=True) as session:
        status = (await session.execute(select(DeviceStatusRecord))).scalar_one()
        gate = (await session.execute(select(SpaceHeatingGateRecord))).scalar_one()
        assert inspect(status).persistent
        assert inspect(gate).persistent
        assert status.outdoor_temp == gate.last_raw_outdoor_c == 5.0
    for row, name in ((status, "outdoor_temp"), (gate, "config_fingerprint")):
        assert inspect(row).detached
        assert inspect(row).expired_attributes
        with pytest.raises(DetachedInstanceError):
            getattr(row, name)
    orm_db.assert_cleanup()


@pytest.fixture
def route_harness(orm_db, monkeypatch):
    """Use production routers/auth/quality/gate/indoor logic; fake only config
    and the unrelated measured-window aggregation.
    """
    from packages.api import _helpers
    from packages.api.auth import require_auth
    from packages.api.routers import models_router, optimizer
    from packages.core import device_data_quality, outcome_experiments, settings_service
    from packages.core.config import settings
    from packages.core.heat_curve import HeatCurveConfig

    values = {
        "aquarea_username": "offline-test",
        "aquarea_password": "offline-test",
        "device_status_max_age_minutes": "15",
        "poll_interval_seconds": "300",
    }
    floats = {
        "outcome_experiment_max_curve_step_c": 0.5,
        "comfort_temp_target": 21.5,
        "comfort_temp_min": 21.0,
        "comfort_temp_max": 22.0,
    }
    enabled = AsyncMock(return_value=True)
    monkeypatch.setattr(outcome_experiments, "get_bool_setting", enabled)
    monkeypatch.setattr(outcome_experiments, "get_all_settings", AsyncMock(return_value=values))
    for module in (settings_service, outcome_experiments):
        monkeypatch.setattr(
            module, "get_float_setting", AsyncMock(side_effect=lambda key: floats[key])
        )
        monkeypatch.setattr(
            module,
            "get_space_heating_gate_config",
            AsyncMock(return_value=HeatingGateConfig()),
        )
    monkeypatch.setattr(
        settings_service, "get_setting", AsyncMock(side_effect=lambda key, **_kw: values[key])
    )
    monkeypatch.setattr(device_data_quality, "get_setting", settings_service.get_setting)
    monkeypatch.setattr(
        settings_service, "get_heat_curve_config", AsyncMock(return_value=HeatCurveConfig())
    )
    monkeypatch.setattr(
        settings_service,
        "get_heat_curve_verification_state",
        AsyncMock(return_value={"status": "verified"}),
    )
    monkeypatch.setattr(settings_service, "get_user_tz", AsyncMock(return_value="UTC"))
    monkeypatch.setattr(_helpers, "get_price_area", AsyncMock(return_value="SE3"))
    monkeypatch.setattr(
        "packages.poller.smartthings.get_selected_device_ids", AsyncMock(return_value=["room"])
    )
    setters = []
    for name in ("set_setting", "set_heat_curve_verification_state"):
        setter = AsyncMock(side_effect=AssertionError("GET must not persist settings"))
        monkeypatch.setattr(settings_service, name, setter)
        setters.append(setter)
    measurement = {
        "cost": {"measured_cost": 1.25},
        "comfort": {"degree_hours": 0.5},
        "weather_matched_comparison": {"status": "insufficient_data"},
    }
    monkeypatch.setattr(optimizer, "measured_window_outcome", AsyncMock(return_value=measurement))
    monkeypatch.setattr(settings, "api_token", "test-token")
    app = FastAPI(dependencies=[Depends(require_auth)])
    app.include_router(optimizer.router)
    app.include_router(models_router.router)

    def seed(case="fresh"):
        now = dt.datetime.now(UTC) - dt.timedelta(seconds=1)
        status, gate = _rows(now)
        indoor = IndoorTempReading(
            timestamp=now, device_id="room", temperature=18.0, is_stale=False
        )
        if case == "stale_status":
            status.ts = now - dt.timedelta(hours=1)
        elif case == "future_status":
            status.ts = now + dt.timedelta(hours=1)
        elif case in {"stale_gate", "future_gate"}:
            gate.source_status_ts = gate.evaluated_at = now + dt.timedelta(
                hours=-1 if case == "stale_gate" else 1
            )
        elif case in {"blocked_gate", "unknown_gate"}:
            gate.state = "BLOCKED" if case == "blocked_gate" else "UNKNOWN"
        elif case == "mismatched_gate":
            gate.config_fingerprint = "different"
        elif case in {"stale_indoor", "future_indoor"}:
            indoor.timestamp = now + dt.timedelta(hours=-1 if case == "stale_indoor" else 1)
        elif case in {"dhw", "cooling", "defrost"}:
            status.device_action = {"dhw": "HEATING_WATER", "cooling": "COOLING"}.get(case)
            status.defrost_active = case == "defrost"
        rows = []
        if case != "missing_status":
            rows.append(status)
        if case != "missing_gate":
            rows.append(gate)
        if case != "missing_indoor":
            rows.append(indoor)
        if case == "disabled":
            enabled.return_value = False
        orm_db.seed(*rows)
        orm_db.guard()

    async def get(path, *, authenticated=True):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                path,
                headers={"Authorization": "Bearer test-token"} if authenticated else {},
            )
        for setter in setters:
            setter.assert_not_awaited()
        return response

    return SimpleNamespace(seed=seed, get=get, measurement=measurement, values=values)


OUTCOMES = "/api/outcomes/summary?days=7"
ADVICE = "/api/thermal/heat-curve-advice"


async def test_g03_outcomes_summary_with_real_orm_rows(orm_db, route_harness):
    route_harness.seed()
    response = await route_harness.get(OUTCOMES)
    assert response.status_code == 200
    payload = response.json()
    assert payload["days"] == 7
    for key, value in route_harness.measurement.items():
        assert payload[key] == value
    experiment = payload["experiment"]
    assert experiment["status"] == "manual_review_ready"
    assert experiment["mode"] == "manual_review_only"
    assert experiment["maximum_curve_step_c"] == 0.5
    assert experiment["conditions"]["outdoor_temp_c"] == 5.0
    assert experiment["conditions"]["space_heating_confirmed"] is True
    assert len(experiment["guardrails"]) == 4
    assert "No heat-pump command" in experiment["guardrails"][0]
    orm_db.assert_cleanup()


async def test_g03_heat_curve_advice_with_real_orm_rows(orm_db, route_harness):
    route_harness.seed()
    response = await route_harness.get(ADVICE)
    assert response.status_code == 200
    payload = response.json()
    assert payload["readings"] == {
        "indoor_temp_c": 18.0,
        "comfort_target_c": 21.5,
        "outdoor_temp_c": 5.0,
        "curve_supply_target_c": 47.0,
        "controller_heating_enabled": True,
    }
    assert payload["status"] == "too_cold"
    assert payload["gate_state"] == "ALLOWED"
    assert payload["gate_reason"] == "test_allowed"
    assert payload["manual_only"] is True
    assert payload["suggested"]["supply_cold_c"] == 49.0
    assert payload["verification"]["status"] == "verified"
    orm_db.assert_cleanup()


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("missing_status", "waiting_for_current_outdoor_temperature"),
        ("stale_status", "device_status_stale"),
        ("future_status", "waiting_for_current_outdoor_temperature"),
        ("missing_gate", "space_heating_gate_timing_unknown"),
        ("stale_gate", "space_heating_gate_stale"),
        ("future_gate", "space_heating_gate_future"),
        ("blocked_gate", "space_heating_gate_blocked"),
        ("unknown_gate", "space_heating_gate_unknown"),
        ("mismatched_gate", "space_heating_gate_unknown"),
        ("dhw", "domestic_hot_water_active"),
        ("cooling", "cooling_active"),
        ("defrost", "defrost_active"),
        ("disabled", "heating_conditions_available"),
    ],
)
async def test_g03_outcome_unavailable_conditions(orm_db, route_harness, case, reason):
    route_harness.seed(case)
    response = await route_harness.get(OUTCOMES)
    assert response.status_code == 200
    experiment = response.json()["experiment"]
    assert experiment["status"] == (
        "disabled" if case == "disabled" else "waiting_for_heating_conditions"
    )
    assert experiment["conditions"]["reason"] == reason
    orm_db.assert_cleanup()


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing_status", "not_controllable"),
        ("stale_status", "insufficient_data"),
        ("future_status", "not_controllable"),
        ("missing_indoor", "insufficient_data"),
        ("stale_indoor", "insufficient_data"),
        ("future_indoor", "insufficient_data"),
        ("missing_gate", "not_controllable"),
        ("blocked_gate", "not_controllable"),
        ("unknown_gate", "not_controllable"),
        ("mismatched_gate", "not_controllable"),
        # Advice intentionally has no gate-age check; outcomes does.
        ("stale_gate", "too_cold"),
        ("future_gate", "too_cold"),
    ],
)
async def test_g03_advice_unavailable_conditions(orm_db, route_harness, case, expected):
    route_harness.seed(case)
    response = await route_harness.get(ADVICE)
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == expected
    assert (payload["suggested"] is not None) is (expected == "too_cold")
    if case in {"missing_status", "stale_status", "future_status"}:
        assert payload["readings"]["outdoor_temp_c"] is None
    if case in {"missing_indoor", "stale_indoor", "future_indoor"}:
        assert payload["readings"]["indoor_temp_c"] is None
    orm_db.assert_cleanup()


@pytest.mark.parametrize("path", [OUTCOMES, ADVICE])
async def test_g03_routes_require_authentication(orm_db, route_harness, path):
    response = await route_harness.get(path, authenticated=False)
    assert response.status_code == 401
    assert not orm_db.sessions


@pytest.mark.parametrize("path", [OUTCOMES, ADVICE])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_g03_query_error_and_cancellation_cleanup(orm_db, route_harness, path, failure):
    route_harness.seed()
    error = failure("injected offline query failure")
    orm_db.injection.error = error
    with pytest.raises(failure) as caught:
        await route_harness.get(path)
    assert caught.value is error
    orm_db.assert_cleanup()


@pytest.mark.parametrize("borrowed", [False, True])
@pytest.mark.parametrize(
    ("case", "age", "reason"),
    [
        ("fresh", 900, None),
        ("stale", 901, "device_status_stale"),
        ("future", None, "device_status_future"),
        ("provenance_stale", 901, "device_status_stale"),
        ("chronology", None, "device_status_clock_chronology"),
        ("malformed", None, "device_status_timing_unknown"),
        ("credentials", 900, "credentials_missing"),
    ],
)
async def test_g03_real_orm_quality_contract(orm_db, route_harness, borrowed, case, age, reason):
    from packages.core.device_data_quality import get_device_data_quality

    now = dt.datetime.now(UTC)
    status, _gate = _rows(now - dt.timedelta(seconds=901 if case == "stale" else 900))
    if case == "future":
        status.ts = now + dt.timedelta(seconds=1)
    elif case == "provenance_stale":
        status.native_policy_provenance = {
            "captured_at": status.ts.isoformat(),
            "timing": {"observed_at": (now - dt.timedelta(seconds=901)).isoformat()},
        }
    elif case == "chronology":
        status.native_policy_provenance = {
            "timing": {
                "observed_at": (now - dt.timedelta(seconds=100)).isoformat(),
                "retrieved_at": (now - dt.timedelta(seconds=101)).isoformat(),
            }
        }
    elif case == "malformed":
        status.native_policy_provenance = {"timing": "invalid"}
    elif case == "credentials":
        route_harness.values["aquarea_password"] = ""
    timestamp = status.ts
    orm_db.seed(status)
    orm_db.guard()
    if borrowed:
        async with database.get_session(read_only=True) as session:
            quality = await get_device_data_quality(
                now=now, session=session, values=route_harness.values
            )
    else:
        quality = await get_device_data_quality(now=now, values=route_harness.values)
    assert quality == {
        "timestamp": timestamp,
        "age_seconds": age,
        "threshold_seconds": 900,
        "credentials_configured": case != "credentials",
        "ready": reason is None,
        "reasons": [reason] if reason else [],
    }
    orm_db.assert_cleanup()
