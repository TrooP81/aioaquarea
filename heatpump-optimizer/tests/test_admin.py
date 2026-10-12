from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.api.routers import admin
from packages.api.routers.admin import (
    MANUAL_COP_SCOPE,
    MODEL_FEEDING_SCOPES,
    RESET_SCOPES,
    ResetRequest,
    reset_data,
)
from fastapi import HTTPException


class _FakeSession:
    """Records delete statements, count queries, and added rows."""

    def __init__(self, count: int = 5):
        self.deleted = []
        self.added = []
        self.queried = []
        self._count = count

    async def scalar(self, stmt):
        self.queried.append(stmt)
        return self._count

    async def execute(self, stmt):
        if stmt.__visit_name__ == "select":
            self.queried.append(stmt)
            return MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        self.deleted.append(stmt)
        return MagicMock()

    def add(self, obj):
        self.added.append(obj)


class _FakeSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


def _patch_session(session):
    return patch(
        "packages.api.routers.admin.get_session",
        return_value=_FakeSessionCtx(session),
    )


class TestResetScopeConfig:
    def test_every_reset_scope_excludes_manual_ledger_and_revision_sequence(self):
        assert all(
            model.__tablename__ != "manual_cop_observations"
            for models in RESET_SCOPES.values()
            for model in models
        )
        assert "manual_cop" not in " ".join(RESET_SCOPES)

    def test_model_feeding_scopes_are_known(self):
        assert MODEL_FEEDING_SCOPES.issubset(set(RESET_SCOPES))

    def test_plans_scope_deletes_children_before_parent(self):
        names = [m.__tablename__ for m in RESET_SCOPES["plans"]]
        assert names.index("plan_actions") < names.index("plans")

    def test_every_reset_scope_excludes_archival_cop_history(self):
        assert all(
            model.__tablename__ != "cop_history"
            for models in RESET_SCOPES.values()
            for model in models
        )


class TestResetEndpoint:
    async def test_all_scopes_retain_manual_monitoring_storage(self):
        session = _FakeSession(count=7)
        with (
            _patch_session(session),
            patch.object(admin, "reset_ml_models", return_value=[]) as reset_models,
        ):
            result = await reset_data(ResetRequest(scopes=list(RESET_SCOPES)))
        reset_models.assert_called_once_with()
        assert "manual_cop_observations" not in result["deleted_rows"]
        assert all(
            "manual_cop" not in str(statement) for statement in session.queried + session.deleted
        )

    @pytest.mark.asyncio
    async def test_unknown_scope_rejected(self):
        with pytest.raises(HTTPException) as exc:
            await reset_data(ResetRequest(scopes=["bogus"]))
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_scope_rejected(self):
        with pytest.raises(HTTPException) as exc:
            await reset_data(ResetRequest(scopes=[]))
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_non_model_scope_does_not_reset_models(self):
        session = _FakeSession(count=3)
        with (
            _patch_session(session),
            patch.object(admin, "reset_ml_models", return_value=[]) as mock_reset,
        ):
            result = await reset_data(ResetRequest(scopes=["prices"]))
        mock_reset.assert_not_called()
        assert result["models_reset"] is False
        assert result["deleted_rows"]["prices"] == 3

    @pytest.mark.asyncio
    async def test_model_feeding_scope_resets_models(self):
        session = _FakeSession(count=10)
        with (
            _patch_session(session),
            patch.object(admin, "reset_ml_models", return_value=[]) as mock_reset,
        ):
            result = await reset_data(ResetRequest(scopes=["indoor_temp"]))
        mock_reset.assert_called_once()
        assert result["models_reset"] is True
        assert result["deleted_models"] == []
        assert result["deleted_rows"]["indoor_temp_reading"] == 10

    @pytest.mark.asyncio
    async def test_reset_models_false_skips_models(self):
        session = _FakeSession()
        with _patch_session(session), patch.object(admin, "reset_ml_models") as mock_reset:
            result = await reset_data(ResetRequest(scopes=["indoor_temp"], reset_models=False))
        mock_reset.assert_not_called()
        assert result["models_reset"] is False

    @pytest.mark.asyncio
    async def test_audit_log_written_after_reset(self):
        session = _FakeSession()
        with _patch_session(session), patch.object(admin, "reset_ml_models", return_value=[]):
            await reset_data(ResetRequest(scopes=["weather"]))
        assert len(session.added) == 1
        assert session.added[0].action == "reset_data"

    async def test_energy_reset_never_reads_or_deletes_cop_history(self):
        session = _FakeSession(count=7)
        with _patch_session(session):
            result = await reset_data(ResetRequest(scopes=["energy"], reset_models=False))

        assert result["deleted_rows"] == {"consumption": 7}
        assert result["total_rows_deleted"] == 7
        assert len(session.deleted) == 1
        assert session.deleted[0].table.name == "consumption"
        assert all(
            "cop_history" not in str(statement) for statement in session.queried + session.deleted
        )

    async def test_reset_invalidates_lifecycle_in_data_transaction_before_memory_reset(self):
        session = _FakeSession()
        events = []
        state = {"epoch": "epoch-1", "revision": 19, "fence": 20}

        @asynccontextmanager
        async def sessions():
            yield session
            events.append("commit")

        async def invalidate(current_session):
            assert current_session is session
            assert session.deleted == []
            events.append("invalidate")
            return state

        def memory_reset():
            assert events == ["invalidate", "commit"]
            events.append("memory_reset")
            return []

        with (
            patch.object(admin, "get_session", sessions),
            patch.object(admin, "invalidate_lifecycle", AsyncMock(side_effect=invalidate)),
            patch.object(admin, "reset_ml_models", side_effect=memory_reset),
        ):
            result = await reset_data(ResetRequest(scopes=["energy"]))

        assert events == ["invalidate", "commit", "memory_reset", "commit"]
        assert result["model_lifecycle"] == state
        assert result["deleted_models"] == []
        assert result["deleted_rows"] == {"consumption": 5}

    async def test_commit_failure_never_clears_memory_models(self):
        session = _FakeSession()

        @asynccontextmanager
        async def failed_commit():
            yield session
            raise OSError("commit acknowledgement failed")

        with (
            patch.object(admin, "get_session", failed_commit),
            patch.object(admin, "invalidate_lifecycle", AsyncMock(return_value={"revision": 19})),
            patch.object(admin, "reset_ml_models") as memory_reset,
            pytest.raises(OSError, match="commit acknowledgement"),
        ):
            await reset_data(ResetRequest(scopes=["energy"]))
        memory_reset.assert_not_called()
        assert not session.added

    async def test_lifecycle_failure_refuses_before_feed_deletion(self):
        from sqlalchemy.exc import SQLAlchemyError

        session = _FakeSession()
        with (
            _patch_session(session),
            patch.object(
                admin,
                "invalidate_lifecycle",
                AsyncMock(side_effect=SQLAlchemyError("private failure")),
            ),
            patch.object(admin, "reset_ml_models") as memory_reset,
            pytest.raises(HTTPException) as error,
        ):
            await reset_data(ResetRequest(scopes=["energy"]))
        assert error.value.status_code == 503
        assert error.value.detail == {"code": "ml_lifecycle_unavailable"}
        assert session.deleted == []
        memory_reset.assert_not_called()


class _ManualResetSession:
    """Records every statement of the standalone manual-ledger reset transaction."""

    def __init__(self, count=182, fail_on=None):
        self.statements = []
        self.added = []
        self.count = count
        self.fail_on = fail_on

    async def execute(self, statement):
        from sqlalchemy.dialects import postgresql

        compiled = str(statement.compile(dialect=postgresql.dialect()))
        if self.fail_on and self.fail_on in compiled:
            from sqlalchemy.exc import OperationalError

            raise OperationalError(compiled, {}, Exception("lock timeout"))
        self.statements.append(compiled)
        return MagicMock()

    async def scalar(self, statement):
        from sqlalchemy.dialects import postgresql

        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return self.count

    def add(self, obj):
        self.added.append(obj)


class TestManualCOPReset:
    def manual_sessions(self, session, events):
        @asynccontextmanager
        async def sessions():
            events.append("begin")
            yield session
            events.append("commit")

        return sessions

    async def test_manual_scope_deletes_all_devices_disables_forecast_and_audits_atomically(self):
        import json

        session = _ManualResetSession(count=182)
        events = []
        with (
            patch.object(admin, "get_session", self.manual_sessions(session, events)),
            patch.object(admin, "invalidate_lifecycle", AsyncMock()) as lifecycle,
            patch.object(admin, "reset_ml_models") as memory_reset,
        ):
            result = await reset_data(ResetRequest(scopes=[MANUAL_COP_SCOPE]))

        assert events == ["begin", "commit"]
        assert session.statements[:3] == [
            "SET LOCAL lock_timeout = '5s'",
            "SET LOCAL statement_timeout = '20s'",
            "LOCK TABLE public.manual_cop_observations IN SHARE ROW EXCLUSIVE MODE",
        ]
        assert "FROM manual_cop_observations" in session.statements[3]
        assert session.statements[4] == "DELETE FROM manual_cop_observations"
        assert session.statements[5].startswith("INSERT INTO settings (key, value")
        assert "ON CONFLICT (key) DO UPDATE SET value = %(param_1)s" in session.statements[5]
        assert len(session.statements) == 6
        joined = "\n".join(session.statements).lower()
        for forbidden in ("sequence", "setval", "restart", "truncate", "where", "plan", "audit"):
            assert forbidden not in joined
        assert len(session.added) == 1
        audit = session.added[0]
        assert (audit.actor, audit.action, audit.result) == ("user", "reset_data", "ok")
        assert json.loads(audit.payload_json) == {
            "scopes": ["manual_cop"],
            "deleted_rows": {"manual_cop_observations": 182},
            "settings": {"manual_cop_monitoring_enabled": "false"},
            "models_reset": False,
            "deleted_models": [],
            "model_lifecycle": None,
        }
        lifecycle.assert_not_called()
        memory_reset.assert_not_called()
        assert result == {
            "status": "ok",
            "scopes": ["manual_cop"],
            "deleted_rows": {"manual_cop_observations": 182},
            "total_rows_deleted": 182,
            "manual_cop_monitoring_enabled": False,
            "models_reset": False,
            "deleted_models": [],
            "model_lifecycle": None,
        }

    async def test_manual_setting_upsert_binds_false_only(self):
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.dialects.postgresql.dml import Insert

        captured = []

        class Capture(_ManualResetSession):
            async def execute(self, statement):
                if isinstance(statement, Insert):
                    captured.append(statement.compile(dialect=postgresql.dialect()).params)
                return await super().execute(statement)

        session = Capture(count=0)
        with patch.object(admin, "get_session", self.manual_sessions(session, [])):
            result = await reset_data(ResetRequest(scopes=["manual_cop"]))
        assert captured == [
            {"key": "manual_cop_monitoring_enabled", "value": "false", "param_1": "false"}
        ]
        assert result["total_rows_deleted"] == 0

    @pytest.mark.parametrize(
        "scopes",
        [
            ["manual_cop", "energy"],
            ["logs", "manual_cop"],
            ["manual_cop", "manual_cop"],
            ["manual_cop", *RESET_SCOPES],
        ],
    )
    async def test_manual_scope_must_be_requested_alone(self, scopes):
        sessions = MagicMock(side_effect=AssertionError("no storage access"))
        with patch.object(admin, "get_session", sessions), pytest.raises(HTTPException) as error:
            await reset_data(ResetRequest(scopes=scopes))
        assert error.value.status_code == 400
        assert error.value.detail == "The manual_cop reset scope must be requested alone"
        sessions.assert_not_called()

    @pytest.mark.parametrize(
        "fail_on", ["LOCK TABLE", "DELETE FROM manual_cop", "INSERT INTO settings"]
    )
    async def test_manual_reset_storage_failure_rolls_back_without_audit(self, fail_on):
        session = _ManualResetSession(fail_on=fail_on)
        events = []

        @asynccontextmanager
        async def sessions():
            events.append("begin")
            try:
                yield session
            except BaseException:
                events.append("rollback")
                raise
            events.append("commit")

        with (
            patch.object(admin, "get_session", sessions),
            patch.object(admin, "reset_ml_models") as memory_reset,
            pytest.raises(HTTPException) as error,
        ):
            await reset_data(ResetRequest(scopes=["manual_cop"]))
        assert error.value.status_code == 503
        assert error.value.detail == {"code": "manual_cop_reset_unavailable"}
        assert events == ["begin", "rollback"]
        assert session.added == []
        memory_reset.assert_not_called()

    async def test_manual_reset_commit_failure_is_not_reported_as_success(self):
        session = _ManualResetSession()

        @asynccontextmanager
        async def failed_commit():
            yield session
            raise OSError("commit acknowledgement failed")

        with (
            patch.object(admin, "get_session", failed_commit),
            pytest.raises(OSError, match="commit acknowledgement"),
        ):
            await reset_data(ResetRequest(scopes=["manual_cop"]))

    def test_broad_scopes_and_select_all_never_include_manual_ledger(self):
        assert MANUAL_COP_SCOPE == "manual_cop"
        assert MANUAL_COP_SCOPE not in RESET_SCOPES
        assert MANUAL_COP_SCOPE not in MODEL_FEEDING_SCOPES


class TestModelResetMethods:
    def test_reset_ml_models_never_enumerates_or_deletes_files(self, tmp_path):
        blocked = tmp_path / "demand_model_blocked.pkl"
        blocked.write_text("x")
        deleted = tmp_path / "demand_model_deleted.pkl"
        deleted.write_text("x")
        with (
            patch("packages.ml.models_common.MODEL_DIR", tmp_path),
            patch.object(type(blocked), "glob", side_effect=AssertionError("glob discovery")),
            patch.object(type(blocked), "unlink", side_effect=AssertionError("artifact deletion")),
            patch("packages.ml.model_status.clear_artifact_status_cache") as clear_cache,
        ):
            deleted_files = admin.reset_ml_models()

        assert deleted_files == []
        assert blocked.read_text() == "x"
        assert deleted.read_text() == "x"
        clear_cache.assert_called_once_with()

    def test_retired_cop_reset_cannot_activate_a_model(self):
        from packages.ml.cop_model_core import COPModel

        m = COPModel()
        m._model = object()
        m._version = "v1"
        m._metrics = {"r2": 0.9}
        m.reset()
        assert m._model is None
        assert m.version == "retired"
        assert m.metrics == {}
        assert m.is_trained is False

    def test_demand_model_reset(self):
        from packages.ml.demand_model_core import DemandModel

        m = DemandModel()
        m._model = object()
        m._version = "v1"
        m.reset()
        assert m._model is None
        assert m._version == "untrained"
        assert m.is_trained is False

    def test_comfort_model_reset(self):
        from packages.ml.comfort_model import DEFAULT_THERMAL_LAG_MINUTES, ComfortModel

        m = ComfortModel()
        m._model = object()
        m._metrics = {"mae": 0.5}
        m._last_trained = dt.datetime.now()
        m._training_samples = 100
        m._thermal_lag_minutes = 999
        m.reset()
        assert m._model is None
        assert m._metrics == {}
        assert m._last_trained is None
        assert m._training_samples == 0
        assert m._thermal_lag_minutes == DEFAULT_THERMAL_LAG_MINUTES

    def test_thermal_model_reset(self):
        from packages.ml.thermal import ThermalModel, ThermalParams

        m = ThermalModel()
        m.params.tank_heating_rate = 999.0
        m.reset()
        assert m.params.tank_heating_rate == ThermalParams().tank_heating_rate

    def test_reset_ml_models_retains_legacy_cop_baselines_and_lifecycle_files(self, tmp_path):
        archives = {
            "cop_model_1.pkl": b"legacy COP bytes",
            "cop_model_weather_dhw_v6_1.pkl": b"unverified DHW COP bytes",
            "mae_baseline_cop.json": b'{"mae": 0.1}',
            "mae_baseline_cop_weather_dhw_v6.json": b'{"mae": 0.2}',
        }
        for name, content in archives.items():
            (tmp_path / name).write_bytes(content)
        demand = tmp_path / "demand_model_1.pkl"
        demand.write_text("x")
        comfort = tmp_path / "comfort_model_1.pkl"
        comfort.write_text("x")
        thermal = tmp_path / "thermal_params_v1.pkl"
        thermal.write_text("x")
        keep = tmp_path / "other.pkl"
        keep.write_text("x")
        lifecycle_directory = tmp_path / "lifecycle"
        lifecycle_directory.mkdir()
        signed = lifecycle_directory / ("a" * 64 + ".pkl")
        signed.write_bytes(b"retained immutable signed test bytes")

        with (
            patch("packages.ml.models_common.MODEL_DIR", tmp_path),
            patch("packages.ml.cop_model_core.MODEL_DIR", tmp_path),
            patch("packages.ml.demand_model_core.MODEL_DIR", tmp_path),
            patch("packages.ml.comfort_model.MODEL_DIR", tmp_path),
            patch("packages.ml.models.cop_model.reset", side_effect=AssertionError("COP reset")),
        ):
            deleted = admin.reset_ml_models()

        assert deleted == []
        assert {name: (tmp_path / name).read_bytes() for name in archives} == archives
        assert (
            demand.read_text()
            == comfort.read_text()
            == thermal.read_text()
            == keep.read_text()
            == "x"
        )
        assert signed.read_bytes() == b"retained immutable signed test bytes"
