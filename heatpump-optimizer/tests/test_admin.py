import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.api.routers import admin
from packages.api.routers.admin import (
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
        self._count = count

    async def scalar(self, stmt):
        return self._count

    async def execute(self, stmt):
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
    def test_model_feeding_scopes_are_known(self):
        assert MODEL_FEEDING_SCOPES.issubset(set(RESET_SCOPES))

    def test_plans_scope_deletes_children_before_parent(self):
        names = [m.__tablename__ for m in RESET_SCOPES["plans"]]
        assert names.index("plan_actions") < names.index("plans")


class TestResetEndpoint:
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
        with _patch_session(session), patch.object(
            admin, "reset_ml_models", return_value=[]
        ) as mock_reset:
            result = await reset_data(ResetRequest(scopes=["prices"]))
        mock_reset.assert_not_called()
        assert result["models_reset"] is False
        assert result["deleted_rows"]["prices"] == 3

    @pytest.mark.asyncio
    async def test_model_feeding_scope_resets_models(self):
        session = _FakeSession(count=10)
        with _patch_session(session), patch.object(
            admin, "reset_ml_models", return_value=["cop_model_x.pkl"]
        ) as mock_reset:
            result = await reset_data(ResetRequest(scopes=["indoor_temp"]))
        mock_reset.assert_called_once()
        assert result["models_reset"] is True
        assert result["deleted_models"] == ["cop_model_x.pkl"]
        assert result["deleted_rows"]["indoor_temp_reading"] == 10

    @pytest.mark.asyncio
    async def test_reset_models_false_skips_models(self):
        session = _FakeSession()
        with _patch_session(session), patch.object(
            admin, "reset_ml_models"
        ) as mock_reset:
            result = await reset_data(
                ResetRequest(scopes=["indoor_temp"], reset_models=False)
            )
        mock_reset.assert_not_called()
        assert result["models_reset"] is False

    @pytest.mark.asyncio
    async def test_audit_log_written_after_reset(self):
        session = _FakeSession()
        with _patch_session(session), patch.object(
            admin, "reset_ml_models", return_value=[]
        ):
            await reset_data(ResetRequest(scopes=["weather"]))
        assert len(session.added) == 1
        assert session.added[0].action == "reset_data"


class TestModelResetMethods:
    def test_cop_model_reset(self):
        from packages.ml.cop_model_core import COPModel

        m = COPModel()
        m._model = object()
        m._version = "v1"
        m._metrics = {"r2": 0.9}
        m.reset()
        assert m._model is None
        assert m._version == "untrained"
        assert m._metrics == {}
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

    def test_reset_ml_models_deletes_files(self, tmp_path):
        cop = (tmp_path / "cop_model_1.pkl")
        cop.write_text("x")
        demand = (tmp_path / "demand_model_1.pkl")
        demand.write_text("x")
        comfort = (tmp_path / "comfort_model_1.pkl")
        comfort.write_text("x")
        keep = (tmp_path / "other.pkl")
        keep.write_text("x")

        with patch("packages.ml.models_common.MODEL_DIR", tmp_path), patch(
            "packages.ml.cop_model_core.MODEL_DIR", tmp_path
        ), patch("packages.ml.demand_model_core.MODEL_DIR", tmp_path), patch(
            "packages.ml.comfort_model.MODEL_DIR", tmp_path
        ):
            deleted = admin.reset_ml_models()

        assert set(deleted) == {
            "cop_model_1.pkl",
            "demand_model_1.pkl",
            "comfort_model_1.pkl",
        }
        assert not cop.exists()
        assert keep.exists()
