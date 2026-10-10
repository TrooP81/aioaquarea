"""Tests for ML models (COP, Demand) and ThermalModel."""

import asyncio
import datetime as dt
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from packages.optimizer import InfeasibleError, SolverTimeoutError


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return None


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    def add(self, *_args, **_kwargs):
        return None

    async def execute(self, *args, **kwargs):
        self.statements.append(args[0] if args else None)
        if self._results:
            return self._results.pop(0)
        return _FakeResult([])


class _FakeSessionCtx:
    def __init__(self, results):
        self._session = _FakeSession(results)

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


def _mock_get_session(results):
    def factory():
        return _FakeSessionCtx(results)

    return factory


class TestManualCOPCalendar:
    def test_retained_table_and_sequence_contract(self):
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateSequence, CreateTable

        from packages.core.models import ManualCOPObservationRecord, manual_cop_revision_seq
        from packages.core.settings_service import SETTINGS_SCHEMA

        table = ManualCOPObservationRecord.__table__
        assert list(table.primary_key.columns.keys()) == ["device_id", "granularity", "period_date"]
        assert not table.foreign_keys
        assert "id" not in table.columns
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "ck_manual_cop_calendar" in ddl
        assert "value > 0 AND value < 'Infinity'" in ddl
        sequence = str(
            CreateSequence(manual_cop_revision_seq).compile(dialect=postgresql.dialect())
        )
        assert "NO CYCLE" in sequence
        assert "MAXVALUE 9223372036854775807" in sequence
        assert SETTINGS_SCHEMA["manual_cop_monitoring_enabled"]["default"] == "false"

    @pytest.mark.parametrize(
        ("period_date", "hours"),
        [(dt.date(2026, 3, 29), 23), (dt.date(2026, 10, 25), 25)],
    )
    def test_day_boundaries_follow_local_calendar(self, period_date, hours):
        from packages.core.cop_monitoring import period_bounds

        start, end = period_bounds("day", period_date, "Europe/Stockholm")
        assert (end - start).total_seconds() == hours * 3600
        assert start.tzinfo == end.tzinfo == dt.timezone.utc

    def test_ambiguous_midnight_uses_earliest_instant(self):
        from packages.core.cop_monitoring import period_bounds

        start, _ = period_bounds("day", dt.date(2026, 11, 1), "America/Havana")
        assert start == dt.datetime(2026, 11, 1, 4, tzinfo=dt.timezone.utc)

    def test_nonexistent_midnight_is_rejected(self):
        from packages.core.cop_monitoring import period_bounds

        with pytest.raises(ValueError, match="nonexistent"):
            period_bounds("day", dt.date(2011, 12, 30), "Pacific/Apia")

    @pytest.mark.parametrize("granularity", ["week", "year"])
    def test_no_week_or_year_ratios(self, granularity):
        from packages.core.cop_monitoring import period_bounds

        with pytest.raises(ValueError, match="Granularity"):
            period_bounds(granularity, dt.date(2026, 10, 1), "UTC")

    def test_selected_october_is_month_not_year(self):
        from packages.core.cop_monitoring import period_bounds

        start, end = period_bounds("month", dt.date(2026, 10, 1), "Europe/Stockholm")
        assert start == dt.datetime(2026, 9, 30, 22, tzinfo=dt.timezone.utc)
        assert end == dt.datetime(2026, 10, 31, 23, tzinfo=dt.timezone.utc)
        with pytest.raises(ValueError, match="first day"):
            period_bounds("month", dt.date(2026, 10, 4), "Europe/Stockholm")

    @pytest.mark.parametrize(
        ("observed_at", "completeness", "reason"),
        [
            (dt.datetime(2026, 10, 4, 8), "partial", "timezone"),
            (dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc), "partial", "between"),
            (dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc), "partial", "between"),
            (dt.datetime(2026, 10, 4, 8, tzinfo=dt.timezone.utc), "closed", "closed"),
        ],
    )
    def test_observation_and_attestation_validation(self, observed_at, completeness, reason):
        from packages.core.cop_monitoring import validate_observation_period

        with pytest.raises(ValueError, match=reason):
            validate_observation_period(
                "day",
                dt.date(2026, 10, 4),
                "Europe/Stockholm",
                observed_at,
                completeness,
                dt.datetime(2026, 10, 4, 12, tzinfo=dt.timezone.utc),
            )

    def test_fixture_values_are_entry_only_and_strict(self):
        from pydantic import ValidationError

        from packages.api.schemas import ManualCOPPut

        body = {
            "observed_at": "2026-10-04T08:00:00Z",
            "completeness": "partial",
            "timezone": "Europe/Stockholm",
            "expected_revision": "0",
        }
        for value in (3.7, 9.7, 3.9):
            assert ManualCOPPut(value=value, **body).value == value
        for value in (True, False, "9.7", float("nan"), float("inf"), 0, -1):
            with pytest.raises(ValidationError):
                ManualCOPPut(value=value, **body)
        assert ManualCOPPut(value=9.7, **(body | {"expected_revision": "9007199254740993"}))
        for value in (0, 9007199254740993, "01", "-1", "9223372036854775808"):
            with pytest.raises(ValidationError):
                ManualCOPPut(value=9.7, **(body | {"expected_revision": value}))
        for field in ("source", "acquisition", "scope", "measured", "accuracy", "energy_boundary"):
            with pytest.raises(ValidationError):
                ManualCOPPut(value=9.7, **(body | {field: "override"}))


class TestManualCOPForecast:
    issue_at = dt.datetime(2026, 10, 4, 8, tzinfo=dt.timezone.utc)

    def observations(self, issue_at=None, observed_hours=8):
        from zoneinfo import ZoneInfo

        from packages.core.cop_monitoring import ManualCOPObservation, period_bounds

        issue_at = issue_at or self.issue_at
        today = issue_at.astimezone(ZoneInfo("Europe/Stockholm")).date()
        rows = []
        for index in range(28):
            period_date = today - dt.timedelta(days=28 - index)
            start, end = period_bounds("day", period_date, "Europe/Stockholm")
            available = end + dt.timedelta(hours=observed_hours)
            rows.append(
                ManualCOPObservation(
                    device_id="fixture-device",
                    granularity="day",
                    period_date=period_date,
                    timezone="Europe/Stockholm",
                    period_start=start,
                    period_end=end,
                    value=3.0 * 1.01**index,
                    observed_at=available,
                    completeness="closed",
                    created_at=available,
                    updated_at=available,
                    revision=str(index + 1),
                )
            )
        return tuple(rows)

    def test_two_step_projection_and_seven_matched_clock_folds(self):
        from packages.core.cop_monitoring import forecast_manual_cop

        result = forecast_manual_cop(self.observations(), self.issue_at, True)
        assert result["status"] == "ready"
        assert result["target_date"] == dt.date(2026, 10, 5)
        assert result["counts"]["fold_training_rows"] == list(range(20, 27))
        assert result["forecast"]["value"] == pytest.approx(3.0 * 1.01**29)
        assert result["forecast"]["log_daily_trend"] == pytest.approx(np.log(1.01))
        assert result["forecast"]["lower"] > 0
        assert "uncalibrated" in result["forecast"]["band_label"]

    def test_empirical_band_linearly_interpolates_seven_nonzero_log_errors(self):
        from dataclasses import replace

        from packages.core.cop_monitoring import forecast_manual_cop

        rows = list(self.observations())
        multipliers = (1.15, 0.76, 1.37, 0.88, 1.08, 0.94, 1.28)
        for index, multiplier in enumerate(multipliers, start=21):
            rows[index] = replace(rows[index], value=rows[index].value * multiplier)
        log_values = np.log([row.value for row in rows])
        errors = []
        for fold in range(7):
            training_count = 20 + fold
            coefficients = np.polyfit(np.arange(training_count), log_values[:training_count], 1)
            predicted = np.polyval(coefficients, 21 + fold)
            errors.append(abs(log_values[21 + fold] - predicted))
        ordered = sorted(errors)
        assert ordered[5] != pytest.approx(ordered[6])
        width = 0.6 * ordered[5] + 0.4 * ordered[6]
        coefficients = np.polyfit(np.arange(28), log_values, 1)
        projected = np.polyval(coefficients, 29)

        result = forecast_manual_cop(tuple(rows), self.issue_at, True)

        assert result["status"] == "ready"
        assert result["counts"]["fold_training_rows"] == list(range(20, 27))
        assert result["forecast"]["value"] == pytest.approx(np.exp(projected), rel=1e-7)
        assert result["forecast"]["lower"] == pytest.approx(np.exp(projected - width), rel=1e-7)
        assert result["forecast"]["upper"] == pytest.approx(np.exp(projected + width), rel=1e-7)

    def test_disabled_never_imports_fit_libraries(self):
        import builtins

        from packages.core.cop_monitoring import forecast_manual_cop

        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.startswith(("numpy", "sklearn")):
                raise AssertionError("Disabled forecast imported ML")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            result = forecast_manual_cop(self.observations(), self.issue_at, False)
        assert result["status"] == "disabled"
        assert result["forecast"] is None

    @pytest.mark.parametrize("completeness", ["partial", "unknown"])
    def test_nonclosed_middle_day_is_not_promoted(self, completeness):
        from dataclasses import replace

        from packages.core.cop_monitoring import forecast_manual_cop

        rows = list(self.observations())
        rows[4] = replace(rows[4], completeness=completeness)
        result = forecast_manual_cop(tuple(rows), self.issue_at, True)
        assert result["status"] == "insufficient_data"
        assert result["counts"]["available_closed_days"] == 27
        assert result["forecast"] is None

    def test_gap_and_deleted_current_revision_have_no_reconstructed_value(self):
        from packages.core.cop_monitoring import forecast_manual_cop

        rows = self.observations()
        result = forecast_manual_cop(rows[:4] + rows[5:], self.issue_at, True)
        assert result["status"] == "insufficient_data"
        assert result["forecast"] is None

    def test_late_bulk_entry_refuses_all_historical_folds(self):
        from dataclasses import replace

        from packages.core.cop_monitoring import forecast_manual_cop

        available = self.issue_at - dt.timedelta(minutes=1)
        rows = tuple(
            replace(row, created_at=available, updated_at=available) for row in self.observations()
        )
        result = forecast_manual_cop(rows, self.issue_at, True)
        assert result["status"] == "insufficient_data"
        assert result["counts"]["available_closed_days"] == 28
        assert result["counts"]["fold_training_rows"] == [0] * 7
        assert result["forecast"] is None

    def test_issue_hour_changes_current_revision_availability(self):
        from dataclasses import replace

        from packages.core.cop_monitoring import forecast_manual_cop

        rows = list(self.observations())
        correction = dt.datetime(2026, 9, 26, 16, tzinfo=dt.timezone.utc)
        rows[0] = replace(rows[0], updated_at=correction, revision="9007199254740993")
        early = forecast_manual_cop(tuple(rows), self.issue_at, True)
        later = forecast_manual_cop(tuple(rows), self.issue_at.replace(hour=17), True)
        assert early["status"] == "insufficient_data"
        assert early["counts"]["fold_training_rows"][0] == 19
        assert early["forecast"] is None
        assert later["status"] == "ready"
        assert later["counts"]["fold_training_rows"][0] == 20
        assert early["fingerprint"] != later["fingerprint"]

    @pytest.mark.parametrize("timestamp_field", ["observed_at", "created_at", "updated_at"])
    @pytest.mark.parametrize("cutoff", ["historical", "live"])
    @pytest.mark.parametrize("microseconds", [0, 1])
    def test_current_revision_microsecond_cutoffs(self, timestamp_field, cutoff, microseconds):
        from dataclasses import replace

        from packages.core.cop_monitoring import forecast_manual_cop, historical_issue_at

        rows = list(self.observations())
        row_index = 0 if cutoff == "historical" else -7
        available_at = (
            historical_issue_at(rows[-7].period_date, self.issue_at, "Europe/Stockholm")
            if cutoff == "historical"
            else self.issue_at
        )
        rows[row_index] = replace(
            rows[row_index],
            revision="9007199254740993",
            **{timestamp_field: available_at + dt.timedelta(microseconds=microseconds)},
        )

        result = forecast_manual_cop(tuple(rows), self.issue_at, True)

        assert result["status"] == ("insufficient_data" if microseconds else "ready")
        expected_live_rows = 27 if cutoff == "live" and microseconds else 28
        assert result["counts"]["available_closed_days"] == expected_live_rows
        if microseconds:
            assert result["forecast"] is None
            if cutoff == "historical":
                assert result["counts"]["fold_training_rows"][0] == 19
        else:
            expected_fold_rows = (
                list(range(20, 27)) if cutoff == "historical" else [20, 21, 21, 22, 23, 24, 25]
            )
            assert result["counts"]["fold_training_rows"] == expected_fold_rows
            assert result["forecast"]["value"] == pytest.approx(3.0 * 1.01**29)

    def test_october_3_issue_excludes_18_hour_correction(self):
        from packages.core.cop_monitoring import historical_issue_at

        issue = historical_issue_at(
            dt.date(2026, 10, 4),
            dt.datetime(2026, 10, 5, 8, 2, 3, 4, tzinfo=dt.timezone.utc),
            "Europe/Stockholm",
        )
        assert issue == dt.datetime(2026, 10, 3, 8, 2, 3, 4, tzinfo=dt.timezone.utc)
        assert dt.datetime(2026, 10, 3, 16, tzinfo=dt.timezone.utc) > issue

    def test_held_out_target_may_arrive_after_historical_but_not_actual_issue(self):
        from dataclasses import replace

        from packages.core.cop_monitoring import forecast_manual_cop

        rows = list(self.observations())
        target = rows[-7]
        available = self.issue_at - dt.timedelta(minutes=1)
        rows[-7] = replace(target, created_at=available, updated_at=available)
        assert forecast_manual_cop(tuple(rows), self.issue_at, True)["status"] == "ready"
        rows[-7] = replace(target, updated_at=self.issue_at + dt.timedelta(minutes=1))
        result = forecast_manual_cop(tuple(rows), self.issue_at, True)
        assert result["status"] == "insufficient_data"
        assert result["forecast"] is None

    @pytest.mark.parametrize("utc_hour", [0, 1])
    def test_ambiguous_historical_issue_uses_live_fold(self, utc_hour):
        from packages.core.cop_monitoring import historical_issue_at

        live = dt.datetime(2026, 10, 25, utc_hour, 30, tzinfo=dt.timezone.utc)
        assert historical_issue_at(dt.date(2026, 10, 26), live, "Europe/Stockholm") == live

    def test_nonexistent_historical_issue_refuses_without_shifting(self):
        from packages.core.cop_monitoring import forecast_manual_cop, historical_issue_at

        issue = dt.datetime(2026, 4, 5, 0, 30, tzinfo=dt.timezone.utc)
        with pytest.raises(ValueError, match="nonexistent_historical_issue_time"):
            historical_issue_at(dt.date(2026, 3, 30), issue, "Europe/Stockholm")
        result = forecast_manual_cop(self.observations(issue, observed_hours=1), issue, True)
        assert result["status"] == "unavailable"
        assert result["reason"] == "nonexistent_historical_issue_time"
        assert result["forecast"] is None

    def test_mixed_timezones_refuse(self):
        from dataclasses import replace

        from packages.core.cop_monitoring import forecast_manual_cop, period_bounds

        rows = list(self.observations())
        start, end = period_bounds("day", rows[4].period_date, "UTC")
        rows[4] = replace(rows[4], timezone="UTC", period_start=start, period_end=end)
        result = forecast_manual_cop(tuple(rows), self.issue_at, True)
        assert result["reason"] == "mixed_timezones"
        assert result["forecast"] is None

    def test_stale_and_month_only_have_no_forecast(self):
        from dataclasses import replace

        from packages.core.cop_monitoring import forecast_manual_cop

        rows = self.observations()
        stale = forecast_manual_cop(rows[:-1], self.issue_at, True)
        assert stale["status"] == "stale"
        assert stale["forecast"] is None
        monthly = forecast_manual_cop((replace(rows[0], granularity="month"),), self.issue_at, True)
        assert monthly["status"] == "no_data"
        assert monthly["forecast"] is None

    def test_missing_optional_library_is_unavailable_not_legacy_fallback(self):
        import builtins

        from packages.core.cop_monitoring import forecast_manual_cop

        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.startswith("sklearn"):
                raise ImportError("Fixture missing optional ML")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            result = forecast_manual_cop(self.observations(), self.issue_at, True)
        assert result["status"] == "unavailable"
        assert result["reason"] == "optional_ml_dependencies_unavailable"
        assert result["forecast"] is None

    @pytest.mark.parametrize("predicted", [float("nan"), float("inf"), -10000.0])
    def test_invalid_projection_never_returns_numbers(self, predicted):
        from packages.core.cop_monitoring import forecast_manual_cop

        estimator = MagicMock(coef_=np.asarray([0.01]))
        estimator.predict.return_value = np.asarray([predicted])
        with patch("sklearn.linear_model.LinearRegression", return_value=estimator):
            result = forecast_manual_cop(self.observations(), self.issue_at, True)
        assert result["status"] == "unavailable"
        assert result["forecast"] is None

    @asynccontextmanager
    async def blocked_workers(self, monkeypatch):
        from packages.core import cop_monitoring

        loop = asyncio.get_running_loop()
        started = asyncio.Queue()
        released = threading.Event()
        stopped = []
        tasks = []
        limit = asyncio.Semaphore(2)
        original_offload = asyncio.to_thread

        def blocked_fit(observations, issue_at, enabled):
            assert isinstance(observations, tuple)
            worker = threading.get_ident()
            loop.call_soon_threadsafe(started.put_nowait, worker)
            try:
                assert released.wait(20), "Owned fitting job was not released"
                return cop_monitoring.monitoring_result(observations, issue_at)
            finally:
                stopped.append(worker)

        async def tracked_offload(function, *arguments):
            tasks.append(asyncio.current_task())
            return await original_offload(function, *arguments)

        monkeypatch.setattr(cop_monitoring, "_FIT_LIMIT", limit)
        monkeypatch.setattr(cop_monitoring, "forecast_manual_cop", blocked_fit)
        monkeypatch.setattr(asyncio, "to_thread", tracked_offload)
        state = SimpleNamespace(
            started=started, released=released, stopped=stopped, tasks=tasks, limit=limit
        )
        try:
            yield state
        finally:
            released.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
            assert len(stopped) == len(tasks)
            assert all(task.done() for task in tasks)
            assert limit._value == 2

    async def test_fitting_timeout_keeps_permit_until_owned_worker_terminates(self, monkeypatch):
        from packages.core.cop_monitoring import forecast_snapshot

        async with self.blocked_workers(monkeypatch) as workers:
            request = asyncio.create_task(
                forecast_snapshot(self.observations(), self.issue_at, True)
            )
            try:
                await asyncio.wait_for(workers.started.get(), timeout=5)
                result = await asyncio.wait_for(request, timeout=8)
                assert result["status"] == "unavailable"
                assert result["reason"] == "forecast_timeout"
                assert result["forecast"] is None
                assert workers.limit._value == 1
                assert len(workers.tasks) == 1
                assert not workers.tasks[0].done()
                assert workers.stopped == []
            finally:
                workers.released.set()
                await asyncio.gather(request, return_exceptions=True)

    async def test_cancelled_workers_retain_both_permits_and_refuse_third_fit(self, monkeypatch):
        from packages.core.cop_monitoring import forecast_snapshot

        async with self.blocked_workers(monkeypatch) as workers:
            requests = [
                asyncio.create_task(forecast_snapshot(self.observations(), self.issue_at, True))
                for _ in range(2)
            ]
            try:
                identities = [
                    await asyncio.wait_for(workers.started.get(), timeout=5) for _ in requests
                ]
                assert len(set(identities)) == 2
                assert workers.limit._value == 0
                for request in requests:
                    request.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await request
                assert workers.limit._value == 0
                assert all(not task.done() for task in workers.tasks)
                result = await forecast_snapshot(self.observations(), self.issue_at, True)
                assert result["status"] == "unavailable"
                assert result["reason"] == "forecast_timeout"
                assert result["forecast"] is None
                assert len(workers.tasks) == 2
                assert workers.started.empty()
                assert workers.stopped == []
                assert workers.limit._value == 0
            finally:
                workers.released.set()
                await asyncio.gather(*requests, return_exceptions=True)

    async def test_disabled_snapshot_never_acquires_or_starts_a_worker(self, monkeypatch):
        from packages.core import cop_monitoring

        limit = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
        offload = AsyncMock(side_effect=AssertionError("Disabled monitoring offloaded a fit"))
        monkeypatch.setattr(cop_monitoring, "_FIT_LIMIT", limit)
        monkeypatch.setattr(asyncio, "to_thread", offload)
        result = await cop_monitoring.forecast_snapshot(self.observations(), self.issue_at, False)
        assert result["status"] == "disabled"
        assert result["forecast"] is None
        limit.acquire.assert_not_awaited()
        limit.release.assert_not_called()
        offload.assert_not_awaited()

    async def test_thread_failure_is_panel_only(self, monkeypatch):
        from packages.core import cop_monitoring

        limit = asyncio.Semaphore(2)
        monkeypatch.setattr(cop_monitoring, "_FIT_LIMIT", limit)

        with patch(
            "packages.core.cop_monitoring.forecast_manual_cop", side_effect=RuntimeError("fixture")
        ):
            result = await cop_monitoring.forecast_snapshot(
                self.observations(), self.issue_at, True
            )
        assert result["status"] == "unavailable"
        assert result["reason"] == "forecast_failed"
        assert result["forecast"] is None
        assert limit._value == 2

        following = await cop_monitoring.forecast_snapshot(self.observations(), self.issue_at, True)
        assert following["status"] == "ready"
        assert following["forecast"]["value"] == pytest.approx(3.0 * 1.01**29)
        assert limit._value == 2

    def test_monitoring_has_no_control_or_artifact_imports(self):
        import ast
        import inspect

        from packages.core import cop_monitoring

        imports = [
            node.module or ""
            for node in ast.walk(ast.parse(inspect.getsource(cop_monitoring)))
            if isinstance(node, ast.ImportFrom)
        ]
        assert not any(
            name.startswith(
                (
                    "packages.optimizer",
                    "packages.ml",
                    "packages.poller",
                    "packages.core.services",
                    "redis",
                )
            )
            for name in imports
        )


class TestCOPModel:
    def test_only_inert_compatibility_methods_remain(self):
        import ast
        import inspect

        from packages.ml import cop_model_core

        tree = ast.parse(inspect.getsource(cop_model_core))
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert imports == ["__future__"]
        model_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        assert {
            node.name
            for node in model_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        } == {
            "__init__",
            "reset",
            "is_trained",
            "metrics",
            "last_data_quality",
            "version",
            "train",
            "predict",
            "predict_cop",
            "load_latest",
        }

    @pytest.mark.parametrize("module_name", ["cop_model_core", "models"])
    async def test_retired_lifecycle_never_accesses_artifacts(self, module_name, tmp_path):
        import importlib

        module = importlib.import_module(f"packages.ml.{module_name}")
        archives = {
            "cop_model_legacy.pkl": b"legacy model",
            "cop_model_weather_dhw_v6_20261001_1200.pkl": b"unverified DHW model",
            "cop_weather_dhw_v6_mae.json": b'{"mae": 0.1}',
        }
        for name, content in archives.items():
            (tmp_path / name).write_bytes(content)

        with (
            patch.object(module, "MODEL_DIR", tmp_path),
            patch.object(Path, "glob", side_effect=AssertionError("COP artifact enumeration")),
            patch.object(Path, "read_bytes", side_effect=AssertionError("COP artifact read")),
            patch("packages.ml.safe_persistence.safe_load") as safe_load,
            patch("packages.ml.safe_persistence.safe_dump") as safe_dump,
            patch("packages.ml.models_common.prune_old_models") as prune,
            patch("packages.ml.models_common.write_mae_baseline") as baseline,
        ):
            model = module.COPModel()
            model._model = MagicMock()
            model._version = "legacy"
            model._metrics = {"samples": 999, "mae": 0.1}
            assert model.is_trained is False
            assert model.version == "retired"
            assert model.metrics == {}
            assert model.last_data_quality == {}
            assert model.load_latest() is False
            assert await model.train() == {
                "status": "retired",
                "supported": False,
                "error": "cop_model_retired",
                "reason": "unverified_thermal_output",
            }
            for predict in (model.predict, model.predict_cop):
                with pytest.raises(RuntimeError, match="^cop_model_retired$"):
                    predict(5.0, 50, 12)
            model._model.predict.assert_not_called()
            model.reset()
            assert model.is_trained is False
            assert model.version == "retired"
            safe_load.assert_not_called()
            safe_dump.assert_not_called()
            prune.assert_not_called()
            baseline.assert_not_called()

        assert {name: (tmp_path / name).read_bytes() for name in archives} == archives

    async def test_direction_computation_refuses_without_session(self):
        from packages.ml.models import DirectionAwareCOP

        with patch("packages.ml.models.get_session", side_effect=AssertionError("COP session")):
            with pytest.raises(RuntimeError, match="^cop_model_retired$"):
                await DirectionAwareCOP().compute_cop_intervals()

    @pytest.mark.parametrize("row", [(3.5, 2, 2.0, 5.0), (None, 0, None, None)])
    async def test_archival_aggregation_is_read_only_and_unverified(self, row):
        from packages.ml.models import DirectionAwareCOP

        result = MagicMock()
        result.one.return_value = row
        session = AsyncMock()
        session.execute.return_value = result
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("packages.ml.models.get_session", return_value=ctx):
            archive = await DirectionAwareCOP().get_average_cop(hours=48, mode="HEATING_WATER")

        assert archive == {
            "avg_cop": row[0],
            "sample_count": row[1],
            "min_cop": row[2],
            "max_cop": row[3],
            "period_hours": 48,
            "mode_filter": "HEATING_WATER",
            "status": "retired",
            "provenance": "legacy_unverified_estimate",
            "confidence": "estimated",
            "measured": False,
        }
        session.execute.assert_awaited_once()
        assert session.execute.call_args.args[0].is_select

    @pytest.mark.parametrize(
        ("model_class", "filename"),
        [
            ("COPModel", "cop_model_weather_dhw_v4_20261001_1200.pkl"),
            ("COPModel", "cop_model_weather_dhw_v5_20261001_1200.pkl"),
            ("DemandModel", "demand_model_weather_v2_20261001_1200.pkl"),
        ],
    )
    def test_load_latest_ignores_legacy_artifact_prefix(self, model_class, filename, tmp_path):
        from packages.ml.models import COPModel, DemandModel

        model = {"COPModel": COPModel, "DemandModel": DemandModel}[model_class]()
        (tmp_path / filename).write_bytes(b"legacy artifact")
        with (
            patch("packages.ml.models.MODEL_DIR", tmp_path),
            patch("packages.ml.safe_persistence.safe_load") as safe_load,
        ):
            assert not model.load_latest()

        safe_load.assert_not_called()


class TestCOPRetirementLifecycle:
    def test_status_tombstone_precedes_cache_and_artifact_inspection(self, tmp_path):
        from packages.ml import model_status

        cached = (None, {"trained": True, "samples": 999, "source_records": 999})
        with (
            patch.dict(model_status._artifact_status_cache, {"cop": cached}),
            patch.object(
                model_status,
                "_artifacts_with_fingerprint",
                side_effect=AssertionError("COP fingerprint"),
            ),
            patch("packages.ml.models.COPModel", side_effect=AssertionError("COP instance")),
            patch("packages.ml.safe_persistence.safe_load") as safe_load,
        ):
            status = model_status._inspect_model_artifact("cop", tmp_path)
            assert status == {
                "trained": False,
                "last_trained": None,
                "samples": 0,
                "source_records": 0,
                "metrics": None,
                "status": "retired",
                "supported": False,
                "unavailable_reason": "retired",
                "reason": "unverified_thermal_output",
            }
            status["trained"] = True
            assert model_status._inspect_model_artifact("cop", tmp_path)["trained"] is False
            safe_load.assert_not_called()

    @pytest.mark.parametrize("trained", [False, True])
    async def test_startup_only_loads_demand_and_keeps_weekly_cadence(self, trained):
        from packages.ml import main as ml_main

        demand = MagicMock(is_trained=trained)
        demand.train = AsyncMock(return_value={"error": "Insufficient data", "samples": 167})
        scheduler = MagicMock()
        enqueue = AsyncMock(return_value={"request_id": "fixture", "status": "queued"})
        with (
            patch.object(ml_main, "demand_model", demand),
            patch("packages.ml.lifecycle.enqueue_maintenance_request", enqueue),
            patch.object(ml_main, "create_scheduler", return_value=scheduler),
            patch("packages.core.logging.configure_logging"),
            patch.object(ml_main.asyncio, "sleep", new=AsyncMock(side_effect=KeyboardInterrupt)),
            patch("packages.ml.models.COPModel.train", side_effect=AssertionError("COP train")),
            patch(
                "packages.ml.models.COPModel.load_latest", side_effect=AssertionError("COP load")
            ),
        ):
            await ml_main.main()

        demand.load_latest.assert_called_once_with()
        demand.train.assert_not_awaited()
        enqueue.assert_awaited_once_with("demand", "ml_startup", automatic=True)
        scheduler.add_job.assert_called_once_with(
            ml_main.retrain_models,
            "cron",
            day_of_week="sun",
            hour=3,
            timezone="UTC",
            id="retrain",
            kwargs={"automatic": True, "source": "ml_schedule"},
        )
        scheduler.shutdown.assert_called_once_with()

    async def test_weekly_and_manual_retrain_only_enqueue_demand(self):
        from packages.ml import main as ml_main

        demand = MagicMock()
        demand.train = AsyncMock(return_value={"version": "fixture"})
        enqueue = AsyncMock(return_value={"request_id": "fixture", "status": "queued"})
        with (
            patch.object(ml_main, "demand_model", demand),
            patch("packages.ml.lifecycle.enqueue_maintenance_request", enqueue),
            patch("packages.ml.models.COPModel.train", side_effect=AssertionError("COP train")),
        ):
            await ml_main.retrain_models()
        enqueue.assert_awaited_once_with("demand", "ml_entrypoint", automatic=False)
        demand.train.assert_not_awaited()

    @pytest.mark.parametrize("usable_samples", [167, 168])
    async def test_seasonal_calibration_only_uses_retained_models(self, usable_samples):
        from packages.ml import models, seasonal_learning
        from packages.ml.thermal import MIN_LEARNED_RATE_SAMPLES

        class RetiredCOP:
            def __getattr__(self, name):
                raise AssertionError(f"COP access: {name}")

        demand = MagicMock(is_trained=False, arefresh_committed=AsyncMock(return_value=False))
        demand.training_data_quality = AsyncMock(
            return_value={
                "usable_samples": usable_samples,
                "minimum_samples": 168,
                "remaining_samples": max(0, 168 - usable_samples),
                "ready_to_train": usable_samples >= 168,
            }
        )

        async def train_demand():
            demand.is_trained = True
            return {"version": "fixture-demand", "samples": usable_samples}

        demand.train = AsyncMock(side_effect=train_demand)
        thermal = MagicMock(arefresh_if_changed=AsyncMock(return_value=False))
        thermal.params.indoor_heating_samples = MIN_LEARNED_RATE_SAMPLES
        thermal.confidence_for.return_value = "learned"
        thermal.calibrate = AsyncMock(return_value={"sample_count": MIN_LEARNED_RATE_SAMPLES})
        status = {"observe_only_active": True, "auto_train": True, "auto_exit": True}
        enqueue = AsyncMock(
            side_effect=lambda component, source, **kwargs: {
                "request_id": component,
                "status": "queued",
            }
        )
        with (
            patch.object(
                seasonal_learning,
                "get_seasonal_calibration_status",
                new=AsyncMock(side_effect=[status, {**status, "observe_only_active": False}]),
            ) as read_status,
            patch.object(seasonal_learning, "get_bool_setting", new=AsyncMock(return_value=True)),
            patch("packages.ml.lifecycle.enqueue_maintenance_request", enqueue),
            patch.object(seasonal_learning, "set_settings_bulk", new=AsyncMock()) as save_settings,
            patch.object(models, "demand_model", demand),
            patch.object(models, "cop_model", RetiredCOP()),
            patch.object(models, "COPModel", side_effect=AssertionError("COP constructor")),
            patch("packages.ml.thermal.thermal_model", thermal),
            patch(
                "packages.ml.cop_model_core.COPModel.train",
                side_effect=AssertionError("COP training"),
            ),
            patch(
                "packages.ml.cop_model_core.COPModel.load_latest",
                side_effect=AssertionError("COP loading"),
            ),
            patch(
                "packages.ml.models.DirectionAwareCOP.compute_cop_intervals",
                side_effect=AssertionError("COP computation"),
            ),
            patch.object(Path, "glob", side_effect=AssertionError("COP artifact enumeration")),
            patch("packages.ml.safe_persistence.safe_load", side_effect=AssertionError("load")),
            patch("packages.ml.safe_persistence.safe_dump", side_effect=AssertionError("publish")),
            patch(
                "packages.ml.models_common.write_mae_baseline",
                side_effect=AssertionError("baseline"),
            ),
            patch(
                "packages.ml.models_common.prune_old_models", side_effect=AssertionError("prune")
            ),
        ):
            result = await seasonal_learning.run_seasonal_calibration_cycle()

        demand.training_data_quality.assert_awaited_once_with()
        demand.arefresh_committed.assert_awaited_once_with()
        thermal.arefresh_if_changed.assert_awaited_once_with()
        thermal.calibrate.assert_not_awaited()
        assert result["demand"]["minimum_samples"] == 168
        assert result["demand"]["usable_samples"] == usable_samples
        assert "cop" not in result
        demand.train.assert_not_awaited()
        save_settings.assert_not_awaited()
        assert read_status.await_count == 1
        assert result["cycle"] == "queued"
        assert result["next_step"] == "await_model_maintenance"
        components = [call.args[0] for call in enqueue.await_args_list]
        assert components == (["thermal", "demand"] if usable_samples == 168 else ["thermal"])
        assert all(call.kwargs["automatic"] is True for call in enqueue.await_args_list)

    async def test_optimizer_startup_loads_retained_models_and_logs_retirement_once(self):
        from packages.optimizer import main as optimizer_main

        demand = MagicMock(is_trained=False)
        comfort = MagicMock(is_trained=False)
        thermal = MagicMock()
        thermal.params.last_calibrated = None
        wrapper = MagicMock()
        wrapper.start = AsyncMock(side_effect=RuntimeError("fixture startup stop"))
        with (
            patch.object(optimizer_main, "_demand_model", demand),
            patch.object(optimizer_main, "comfort_model", comfort),
            patch.object(optimizer_main, "thermal_model", thermal),
            patch.object(optimizer_main, "AquareaWrapper", return_value=wrapper),
            patch.object(
                optimizer_main,
                "get_device_data_quality",
                new=AsyncMock(return_value={"ready": False, "reasons": ["fixture"]}),
            ),
            patch.object(optimizer_main, "logger") as logger,
            patch("packages.core.logging.configure_logging"),
            patch(
                "packages.ml.models.COPModel.load_latest", side_effect=AssertionError("COP load")
            ),
            patch("packages.ml.models.COPModel.train", side_effect=AssertionError("COP train")),
        ):
            with pytest.raises(RuntimeError, match="fixture startup stop"):
                await optimizer_main.main()

        demand.load_latest.assert_called_once_with()
        comfort.load_latest.assert_called_once_with()
        thermal.load_latest.assert_called_once_with()
        retirement_events = [
            entry for entry in logger.info.call_args_list if entry.args == ("cop_retired",)
        ]
        assert len(retirement_events) == 1
        assert retirement_events[0].kwargs == {
            "reason": "unverified_thermal_output",
            "cop_source": "assumed",
            "cop_measured": False,
        }


class TestDemandModel:
    @staticmethod
    def _lifecycle_preprocessing_rows(hours=24):
        base = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(hours=index),
                heat_kwh=float(index),
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=5.0,
                source_date=base.date(),
            )
            for index in range(hours + 1)
        ]
        statuses = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=5 * index),
                space_heating_active=True,
                space_heating_evidence="confirmed",
                defrost_active=False,
            )
            for index in range(hours * 12 + 1)
        ]
        weather = [
            SimpleNamespace(
                ts=base + dt.timedelta(hours=index), temperature=5.0, wind_speed=3.0, irradiance=0.0
            )
            for index in range(hours + 1)
        ]
        return [_FakeResult(consumption), _FakeResult(weather), _FakeResult(statuses)]

    async def test_demand_materialization_yields_before_interval_preprocessing(self, monkeypatch):
        import threading
        from packages.ml.demand_model_core import DemandModel
        from packages.ml.thermal import ThermalModel

        event_thread = threading.get_ident()
        preprocessing_threads = []
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        original = ThermalModel._continuous_indoor_state

        def checked_state(*args, **kwargs):
            preprocessing_threads.append(threading.get_ident())
            return original(*args, **kwargs)

        monkeypatch.setattr(ThermalModel, "_continuous_indoor_state", checked_state)
        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session(self._lifecycle_preprocessing_rows()),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            data = await DemandModel().materialize_candidate()
        assert data is not None and len(data["targets"]) >= 20
        assert heartbeat.is_set(), "materialization never yielded to the event-loop heartbeat"
        assert preprocessing_threads and event_thread not in preprocessing_threads

    @pytest.mark.parametrize("hours", [12, 24])
    async def test_demand_status_history_is_indexed_once(self, monkeypatch, hours):
        import builtins
        from packages.ml.demand_model_core import DemandModel
        from packages.ml import thermal

        full_sorts = MagicMock(wraps=builtins.sorted)
        monkeypatch.setattr(thermal, "sorted", full_sorts, raising=False)
        intervals = []
        original = thermal.ThermalModel._continuous_indoor_state

        def checked_state(*args, **kwargs):
            intervals.append(kwargs.get("status_times"))
            return original(*args, **kwargs)

        monkeypatch.setattr(thermal.ThermalModel, "_continuous_indoor_state", checked_state)
        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session(self._lifecycle_preprocessing_rows(hours)),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            data = await DemandModel().materialize_candidate()
        assert data is not None and len(data["targets"]) >= hours - 1
        assert full_sorts.call_count <= 1
        assert intervals and all(times is not None for times in intervals)
        assert len({id(times) for times in intervals}) == 1

    async def test_confirmed_zero_status_history_is_indexed_once(self, monkeypatch):
        import builtins
        from packages.ml.demand_model_core import DemandModel
        from packages.ml import thermal

        results = self._lifecycle_preprocessing_rows()
        for row in results[0]._rows:
            row.heat_kwh = 0.0
        for status in results[2]._rows:
            status.space_heating_active = False
        full_sorts = MagicMock(wraps=builtins.sorted)
        monkeypatch.setattr(thermal, "sorted", full_sorts, raising=False)
        with (
            patch("packages.ml.demand_model_core.get_session", _mock_get_session(results)),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            data = await DemandModel().materialize_candidate()
        assert data is not None and len(data["targets"]) == 23
        assert np.all(data["targets"] == 0)
        assert full_sorts.call_count <= 1

    async def test_cancelled_preprocessing_retains_worker_until_exit(self, monkeypatch):
        from packages.ml.demand_model_core import DemandModel

        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        original = DemandModel._preprocess_materialized

        def paused(data):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5), "test failed to release preprocessing worker"
            return original(data)

        monkeypatch.setattr(DemandModel, "_preprocess_materialized", staticmethod(paused))
        model = DemandModel()
        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session(self._lifecycle_preprocessing_rows()),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            task = asyncio.create_task(model.materialize_candidate())
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                yielded = asyncio.Event()
                loop.call_soon(yielded.set)
                await yielded.wait()
                assert not task.done() and model._sample_bounds == []
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert model._sample_bounds == []

    @pytest.mark.parametrize("request_count", [8, 50])
    async def test_demand_status_overload_materializes_only_once(self, monkeypatch, request_count):
        from packages.ml import demand_model_core as demand

        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        original = demand.DemandModel._preprocess_materialized

        def paused(data):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(10), "test did not release diagnostic worker"
            return original(data)

        factory = MagicMock(wraps=_mock_get_session(self._lifecycle_preprocessing_rows()))
        timezone = AsyncMock(return_value="UTC")
        monkeypatch.setattr(demand, "get_session", factory)
        monkeypatch.setattr(demand, "get_user_tz", timezone)
        monkeypatch.setattr(demand.DemandModel, "_preprocess_materialized", staticmethod(paused))
        models = [demand.DemandModel() for _ in range(request_count)]
        tasks = [asyncio.create_task(model.training_data_quality()) for model in models]
        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.sleep(0)
            assert factory.call_count == 1, (
                "status callers materialized separate history-sized jobs"
            )
            assert timezone.await_count == 1
        finally:
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, dict) for result in results)
        assert all(result == results[0] for result in results)
        assert all(model.last_data_quality == results[0] for model in models)

    async def test_cancelled_status_waiter_releases_immediately_without_new_inputs(
        self, monkeypatch
    ):
        from packages.ml import demand_model_core as demand

        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        original = demand.DemandModel._preprocess_materialized

        def paused(data):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(10), "test did not release diagnostic worker"
            return original(data)

        factory = MagicMock(wraps=_mock_get_session(self._lifecycle_preprocessing_rows()))
        monkeypatch.setattr(demand, "get_session", factory)
        monkeypatch.setattr(demand, "get_user_tz", AsyncMock(return_value="UTC"))
        monkeypatch.setattr(demand.DemandModel, "_preprocess_materialized", staticmethod(paused))
        running = asyncio.create_task(demand.DemandModel().training_data_quality())
        waiting = None
        try:
            await asyncio.wait_for(started.wait(), 2)
            waiting = asyncio.create_task(demand.DemandModel().training_data_quality())
            await asyncio.sleep(0)
            waiting.cancel()
            yielded = asyncio.Event()
            loop.call_soon(yielded.set)
            await yielded.wait()
            assert waiting.done() and waiting.cancelled(), "cancelled waiter retained its backlog"
            assert not running.done(), "running worker lost ownership before exit"
            assert factory.call_count == 1
        finally:
            release.set()
            await asyncio.gather(
                *[task for task in (running, waiting) if task is not None], return_exceptions=True
            )

    async def test_diagnostic_materialization_uses_lifecycle_row_and_time_bounds(self):
        from packages.ml.demand_model_core import DemandModel

        cutoff = dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc)
        context = _FakeSessionCtx([_FakeResult([]), _FakeResult([])])
        with (
            patch("packages.ml.demand_model_core.get_session", return_value=context),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            await DemandModel()._prepare_data(cutoff=cutoff)
        assert len(context._session.statements) == 2
        for statement in context._session.statements:
            params = statement.compile().params
            assert 50_001 in params.values(), "diagnostic query has no overflow sentinel limit"
            assert cutoff in params.values()
        assert (
            cutoff - dt.timedelta(days=90)
            in context._session.statements[0].compile().params.values()
        )
        assert (
            cutoff - dt.timedelta(days=90, hours=2)
            in context._session.statements[1].compile().params.values()
        )

    async def test_diagnostic_materialization_rejects_overflow_before_preprocessing(self):
        from packages.ml.demand_model_core import DemandModel

        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session([_FakeResult([SimpleNamespace()] * 50_001), _FakeResult([])]),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
            patch.object(DemandModel, "_preprocess_materialized") as preprocess,
        ):
            with pytest.raises(ValueError, match="Demand materialization overflow"):
                await DemandModel().training_data_quality()
        preprocess.assert_not_called()

    async def test_cancelled_preparation_before_materialization_releases_admission(
        self, monkeypatch
    ):
        from packages.ml import demand_model_core as demand

        started = asyncio.Event()

        async def timezone():
            started.set()
            await asyncio.Future()

        factory = MagicMock(side_effect=AssertionError("cancelled admission read history"))
        monkeypatch.setattr(demand, "get_user_tz", timezone)
        monkeypatch.setattr(demand, "get_session", factory)
        task = asyncio.create_task(demand.DemandModel().training_data_quality())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        factory.assert_not_called()
        assert asyncio.get_running_loop() not in demand._PREPARATION_FLIGHTS

    async def test_incompatible_preparation_is_busy_without_materializing(self, monkeypatch):
        from packages.ml import demand_model_core as demand

        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        original = demand.DemandModel._preprocess_materialized

        def paused(data):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(10), "test did not release lifecycle worker"
            return original(data)

        factory = MagicMock(wraps=_mock_get_session(self._lifecycle_preprocessing_rows()))
        monkeypatch.setattr(demand, "get_session", factory)
        monkeypatch.setattr(demand, "get_user_tz", AsyncMock(return_value="UTC"))
        monkeypatch.setattr(demand.DemandModel, "_preprocess_materialized", staticmethod(paused))
        task = asyncio.create_task(demand.DemandModel().materialize_candidate())
        try:
            await asyncio.wait_for(started.wait(), 2)
            status = await demand.DemandModel().training_data_quality()
            assert status["training_blocker"] == "preparation_busy"
            assert status["ready_to_train"] is False
            assert factory.call_count == 1 and not task.done()
        finally:
            release.set()
            await task

    def test_untrained_uses_fallback(self):
        """Untrained model should produce reasonable fallback predictions."""
        from packages.ml.models import DemandModel

        model = DemandModel()
        assert not model.is_trained

        weather = [{"temperature": 5.0, "wind_speed": 3.0, "irradiance": 0.0}] * 24
        predictions = model.predict_hourly(weather, hours=24)

        assert len(predictions) == 24
        assert all(p >= 0 for p in predictions)

    def test_colder_weather_higher_demand(self):
        """Colder weather should predict higher demand in fallback mode."""
        from packages.ml.models import DemandModel

        model = DemandModel()

        cold_weather = [{"temperature": -5.0}] * 24
        warm_weather = [{"temperature": 15.0}] * 24

        cold_demand = model.predict_hourly(cold_weather, hours=24)
        warm_demand = model.predict_hourly(warm_weather, hours=24)

        assert sum(cold_demand) > sum(warm_demand)

    def test_make_features_shape_and_order(self):
        """Shared feature builder must return the fixed 10-feature schema."""
        from packages.ml.models import DemandModel

        features = DemandModel._make_features(5.0, 3.0, 100.0, 12, 2, 1.2, 73.0, 0.8)
        assert features.shape == (10,)
        assert features[0] == 5.0  # temperature
        assert features[1] == 3.0  # wind
        assert features[2] == 100.0  # irradiance
        assert features[3] == 1.2  # precipitation
        assert features[4] == 73.0  # humidity
        assert features[5] == 0.8  # cloud_cover

    @pytest.mark.asyncio
    async def test_prepare_data_uses_interval_rate_not_cumulative(self):
        """Target must be per-interval hourly RATE, never the cumulative counter."""
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one", ts=base, heat_kwh=0.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=2.0
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.5,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=30),
                heat_kwh=1.5,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        results = [_FakeResult(consumption), _FakeResult(weather)]

        model = DemandModel()
        with (
            patch("packages.ml.demand_model_core.get_session", _mock_get_session(results)),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            X, y = await model._prepare_data()

        # 0.5 kWh / 0.25h = 2.0 kW; 1.0 kWh / 0.25h = 4.0 kW
        assert len(y) == 2
        assert sorted(round(float(v), 6) for v in y) == [2.0, 4.0]
        # The cumulative reading (1.5) must NOT leak through as a target.
        assert max(y) == pytest.approx(4.0)
        # Matched weather temperature is used in the demand feature schema.
        assert X[0][0] == 1.0

    @pytest.mark.asyncio
    async def test_prepare_data_uses_source_date_for_historical_local_cutover(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 7, 15, 23, 50, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base,
                heat_kwh=10.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
                source_date=None,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.3,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
                source_date=dt.date(2026, 7, 16),
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=30),
                heat_kwh=0.4,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
                source_date=dt.date(2026, 7, 16),
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        context = _FakeSessionCtx([_FakeResult(consumption), _FakeResult(weather)])
        model = DemandModel()

        with (
            patch("packages.ml.demand_model_core.get_session", return_value=context),
            patch(
                "packages.ml.demand_model_core.get_user_tz",
                new=AsyncMock(return_value="Europe/Stockholm"),
            ),
        ):
            _, target = await model._prepare_data()

        assert target.tolist() == pytest.approx([3.6, 0.4])
        assert model.last_data_quality["counter_source_day_reset"] == 1

    @pytest.mark.asyncio
    async def test_prepare_data_selects_source_date_for_historical_local_cutover(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 7, 15, 23, 50, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base,
                heat_kwh=10.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
                source_date=None,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.3,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
                source_date=dt.date(2026, 7, 16),
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=30),
                heat_kwh=0.4,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
                source_date=dt.date(2026, 7, 16),
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        context = _FakeSessionCtx([_FakeResult(consumption), _FakeResult(weather)])
        model = DemandModel()

        with (
            patch("packages.ml.demand_model_core.get_session", return_value=context),
            patch(
                "packages.ml.demand_model_core.get_user_tz",
                new=AsyncMock(return_value="Europe/Stockholm"),
            ),
        ):
            await model._prepare_data()

        assert "source_date" in str(context._session.statements[0])

    @pytest.mark.asyncio
    async def test_prepare_data_uses_elapsed_time_across_repeated_daily_totals(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15 * index),
                heat_kwh=1.0 if index < 12 else 2.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            )
            for index in range(13)
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        model = DemandModel()
        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session([_FakeResult(consumption), _FakeResult(weather)]),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            _, target = await model._prepare_data()

        assert target.tolist() == pytest.approx([1.0 / 3.0])

    @pytest.mark.asyncio
    async def test_prepare_data_falls_back_to_weather_temp(self):
        """When consumption lacks outdoor_temp, use nearest weather temperature."""
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base,
                heat_kwh=0.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=None,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.25,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=None,
            ),
        ]
        weather = [
            SimpleNamespace(
                ts=base + dt.timedelta(minutes=20), temperature=-3.0, wind_speed=5.0, irradiance=0.0
            )
        ]
        results = [_FakeResult(consumption), _FakeResult(weather)]

        model = DemandModel()
        with (
            patch("packages.ml.demand_model_core.get_session", _mock_get_session(results)),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            X, y = await model._prepare_data()

        assert len(y) == 1
        assert X[0][0] == -3.0  # weather temperature filled in
        assert X[0][1] == 5.0  # weather wind

    @pytest.mark.asyncio
    async def test_hourly_counter_steps_yield_hourly_rates(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15 * index),
                heat_kwh=index // 4,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            )
            for index in range(9)
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        model = DemandModel()
        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session([_FakeResult(consumption), _FakeResult(weather)]),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            _, target = await model._prepare_data()

        assert target.tolist() == pytest.approx([1.0, 1.0])
        assert model.last_data_quality["counter_zero_delta"] == 6

    @pytest.mark.asyncio
    async def test_prepare_data_rejects_nonpositive_and_implausible_rates(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one",
                ts=base,
                heat_kwh=0.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(hours=1),
                heat_kwh=0.5,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(hours=2),
                heat_kwh=100.5,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]

        model = DemandModel()
        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session([_FakeResult(consumption), _FakeResult(weather)]),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
            patch("packages.ml.demand_model_core._logger.info") as log_info,
        ):
            _, target = await model._prepare_data()

        assert target.tolist() == pytest.approx([0.5])
        assert model.last_data_quality["intervals"] == 2
        assert model.last_data_quality["rejected_rate_bounds"] == 1
        assert "one" not in repr(log_info.call_args)


class TestDemandEvidenceBoundaries:
    def test_published_fit_is_evaluated_fit(self):
        import pickle
        from packages.ml.demand_model_core import build_demand_candidate

        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        features = np.zeros((500, 10))
        features[:, 0] = np.tile(np.arange(-5.0, 20.0), 20)
        targets = np.maximum(0.0, 3.0 - 0.1 * features[:, 0])
        bounds = [
            (start + dt.timedelta(hours=index), start + dt.timedelta(hours=index + 1))
            for index in range(len(targets))
        ]
        candidate = build_demand_candidate(
            {"features": features, "targets": targets, "bounds": bounds, "device_id": "one"}
        )
        assert candidate is not None
        restored = pickle.loads(pickle.dumps(candidate))
        for name in ("median", "lower", "upper"):
            np.testing.assert_array_equal(
                restored[name].predict(candidate["evaluation"]["features"]),
                candidate["evaluation"]["predictions"][name],
            )
        assert candidate["metrics"]["fit_samples"] >= 168
        assert dt.datetime.fromisoformat(candidate["evaluation"]["fit_label_end"]) + dt.timedelta(
            hours=4
        ) <= dt.datetime.fromisoformat(candidate["evaluation"]["holdout_issue_start"])

    def test_equal_counters_without_fresh_continuity_are_not_zero(self):
        from zoneinfo import ZoneInfo
        from packages.ml.models_common import iter_confirmed_zero_intervals

        start = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        counters = [
            SimpleNamespace(
                device_id="one",
                ts=start + dt.timedelta(minutes=15 * index),
                heat_kwh=1.0,
                source_date=start.date(),
            )
            for index in range(2)
        ]
        assert list(iter_confirmed_zero_intervals(counters, [], ZoneInfo("UTC"))) == []
        stale = [
            SimpleNamespace(
                device_id="one", ts=start - dt.timedelta(hours=1), space_heating_active=False
            )
        ]
        assert list(iter_confirmed_zero_intervals(counters, stale, ZoneInfo("UTC"))) == []
        fresh = [
            SimpleNamespace(
                device_id="one",
                ts=start + dt.timedelta(minutes=5 * index),
                space_heating_active=False,
            )
            for index in range(4)
        ]
        assert len(list(iter_confirmed_zero_intervals(counters, fresh, ZoneInfo("UTC")))) == 1
        counters[-1].source_date = start.date() - dt.timedelta(days=1)
        assert list(iter_confirmed_zero_intervals(counters, fresh, ZoneInfo("UTC"))) == []

    @pytest.mark.parametrize(
        ("timezone_name", "start"),
        [
            ("Europe/Stockholm", dt.datetime(2026, 10, 1, 22, tzinfo=dt.timezone.utc)),
            ("America/New_York", dt.datetime(2026, 10, 2, 3, tzinfo=dt.timezone.utc)),
        ],
    )
    def test_confirmed_zeros_require_device_local_source_day(self, timezone_name, start):
        from zoneinfo import ZoneInfo
        from packages.ml.models_common import iter_confirmed_zero_intervals

        timezone = ZoneInfo(timezone_name)
        source_day = start.astimezone(timezone).date()
        counters = [
            SimpleNamespace(
                device_id="one",
                ts=start + dt.timedelta(minutes=15 * index),
                heat_kwh=1.0,
                source_date=source_day,
            )
            for index in range(2)
        ]
        fresh = [
            SimpleNamespace(
                device_id="one",
                ts=start + dt.timedelta(minutes=5 * index),
                space_heating_active=False,
            )
            for index in range(4)
        ]
        assert len(list(iter_confirmed_zero_intervals(counters, fresh, timezone))) == 1
        for counter in counters:
            counter.source_date = start.date()
        assert list(iter_confirmed_zero_intervals(counters, fresh, timezone)) == []

    def test_sensor_and_counter_evidence_are_device_scoped(self):
        from zoneinfo import ZoneInfo
        from packages.ml.models_common import iter_confirmed_zero_intervals

        start = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        counters = [
            SimpleNamespace(
                device_id="one",
                ts=start + dt.timedelta(minutes=15 * index),
                heat_kwh=1.0,
                source_date=start.date(),
            )
            for index in range(2)
        ]
        other_device = [
            SimpleNamespace(
                device_id="two",
                ts=start + dt.timedelta(minutes=5 * index),
                space_heating_active=False,
            )
            for index in range(4)
        ]
        assert list(iter_confirmed_zero_intervals(counters, other_device, ZoneInfo("UTC"))) == []

    @pytest.mark.parametrize(("samples", "trained"), [(167, False), (168, True)])
    async def test_demand_minimum_remains_168(self, samples, trained):
        from packages.ml.demand_model_core import (
            DemandModel,
            MIN_TRAINING_SAMPLES,
            build_demand_candidate,
        )

        assert MIN_TRAINING_SAMPLES == 168
        model = DemandModel()
        total = samples + 47
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        features = np.zeros((total, 10))
        features[:, 0] = np.resize(np.arange(-5.0, 20.0), total)
        targets = 3.0 - 0.1 * features[:, 0]
        candidate = build_demand_candidate(
            {
                "features": features,
                "targets": targets,
                "bounds": [
                    (start + dt.timedelta(hours=index), start + dt.timedelta(hours=index + 1))
                    for index in range(total)
                ],
                "device_id": "one",
            }
        )
        assert (candidate is not None) is trained
        assert model.is_trained is False
        if trained:
            assert candidate["metrics"]["fit_samples"] == 168
            assert candidate["metrics"]["holdout_samples"] >= 24

    async def test_demand_without_skill_over_naive_mean_is_not_deployed(self):
        from packages.ml.demand_model_core import DemandModel

        model = DemandModel()
        regressor = MagicMock()
        with (
            patch.object(
                model,
                "_prepare_data",
                new=AsyncMock(return_value=(np.zeros((200, 10)), np.ones(200))),
            ),
            patch.object(model, "_make_quantile_model", return_value=regressor),
            patch("packages.ml.demand_model_core.time_series_cv_mae", return_value=(0.98, 0.1)),
            patch("packages.ml.demand_model_core.time_series_cv_naive_mae", return_value=1.0),
            patch("packages.ml.safe_persistence.safe_dump") as publish,
        ):
            result = await model.train()

        assert result["status"] == "no_skill"
        assert result["skill_vs_naive"] == pytest.approx(0.02)
        assert model.is_trained is False
        regressor.fit.assert_not_called()
        publish.assert_not_called()

    @pytest.mark.parametrize(
        ("status", "mae", "deploy"), [("no_skill", 0.98, True), ("regressed", 0.5, False)]
    )
    async def test_rejected_retrain_does_not_keep_unvalidated_loaded_demand(
        self, status, mae, deploy
    ):
        from packages.ml.demand_model_core import DemandModel

        model = DemandModel()
        deployed = MagicMock()
        deployed.predict.return_value = np.array([99.0])
        model._model = deployed
        model._model_lower = deployed
        model._model_upper = deployed
        model._version = "20260930_0000"
        model._metrics = {"mae": 0.1, "skill_vs_naive": 0.2}
        candidate = MagicMock()
        with (
            patch.object(
                model,
                "_prepare_data",
                new=AsyncMock(return_value=(np.zeros((200, 10)), np.ones(200))),
            ),
            patch.object(model, "_make_quantile_model", return_value=candidate),
            patch("packages.ml.demand_model_core.time_series_cv_mae", return_value=(mae, 0.01)),
            patch("packages.ml.demand_model_core.time_series_cv_naive_mae", return_value=1.0),
            patch(
                "packages.ml.demand_model_core.evaluate_regression",
                return_value={"deploy": deploy, "baseline_mae": 0.1},
            ),
            patch("packages.ml.safe_persistence.safe_dump") as publish,
        ):
            result = await model.train()

        assert result["status"] == status
        candidate.fit.assert_not_called()
        publish.assert_not_called()
        assert model.predict_hourly([{"temperature": 5.0}], hours=1) == [2.5]
        assert model.is_trained is False

    def test_naive_baseline_uses_training_fold_means(self):
        from packages.ml.models_common import time_series_cv_naive_mae

        y = np.array([1.0] * 10 + [3.0] * 10)

        assert time_series_cv_naive_mae(y, max_splits=1) == pytest.approx(1.0)
        assert time_series_cv_naive_mae(np.full(30, 2.0)) == 0.0

    async def test_demand_interleaved_devices_keep_independent_electrical_counters(self):
        from packages.ml.demand_model_core import DemandModel, DEMAND_COUNTER_CONFIRMATION_ENABLED

        assert DEMAND_COUNTER_CONFIRMATION_ENABLED is False
        base = dt.datetime(2026, 1, 5, 8, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(
                device_id=device_id,
                ts=base + dt.timedelta(hours=hour),
                heat_kwh=energy,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=5.0,
                source_date=None,
            )
            for hour, device_id, energy in [
                (0, "one", 100.0),
                (0, "two", 900.0),
                (1, "one", 101.0),
                (1, "two", 903.0),
            ]
        ]
        model = DemandModel()
        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session([_FakeResult(rows), _FakeResult([])]),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            features, target = await model._prepare_data()

        assert len(features) == 2
        assert target.tolist() == pytest.approx([1.0, 3.0])

    @pytest.mark.parametrize(
        ("timezone", "start", "end"),
        [
            (
                "Europe/Amsterdam",
                dt.datetime(2026, 3, 29, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 3, 29, 3, tzinfo=dt.timezone.utc),
            ),
            (
                "Europe/Amsterdam",
                dt.datetime(2026, 10, 25, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 10, 25, 3, tzinfo=dt.timezone.utc),
            ),
            (
                "Europe/Stockholm",
                dt.datetime(2026, 3, 29, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 3, 29, 3, tzinfo=dt.timezone.utc),
            ),
            (
                "Europe/Stockholm",
                dt.datetime(2026, 10, 25, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 10, 25, 3, tzinfo=dt.timezone.utc),
            ),
        ],
    )
    def test_dst_spring_and_fall_use_elapsed_instants(self, timezone, start, end):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        rows = [
            SimpleNamespace(device_id="one", ts=start, heat_kwh=1.0),
            SimpleNamespace(device_id="one", ts=end, heat_kwh=2.0),
        ]
        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo(timezone), max_interval_hours=4.0
            )
        )
        assert intervals[0].elapsed_hours == 3.0


class TestConsumptionIntervals:
    def test_counter_changes_ignore_zero_polls_and_keep_devices_independent(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=1.0),
            SimpleNamespace(
                device_id="two",
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=4.0,
            ),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=1.0),
            SimpleNamespace(device_id="two", ts=base + dt.timedelta(minutes=30), heat_kwh=4.5),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=1), heat_kwh=1.4),
        ]
        diagnostics: dict[str, int] = {}
        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo("UTC"), diagnostics=diagnostics
            )
        )
        assert [item.device_id for item in intervals] == ["two", "one"]
        assert [item.energy_kwh for item in intervals] == pytest.approx([0.5, 0.4])
        assert [item.elapsed_hours for item in intervals] == pytest.approx([0.25, 1.0])
        assert diagnostics["zero_delta"] == 1

    def test_transient_midday_decrease_retains_anchor_for_recovery(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=2.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=1.5),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=1), heat_kwh=2.5),
        ]
        diagnostics: dict[str, int] = {}
        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo("UTC"), diagnostics=diagnostics
            )
        )
        assert intervals[0].energy_kwh == pytest.approx(0.5)
        assert intervals[0].elapsed_hours == pytest.approx(1.0)
        assert diagnostics["midday_decrease"] == 1

    def test_persisted_midday_decrease_reanchors_after_confirmation(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=2.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=1.5),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=30), heat_kwh=1.5),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=1), heat_kwh=2.0),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo("Europe/Stockholm"), diagnostics=diagnostics
            )
        )

        assert [interval.energy_kwh for interval in intervals] == pytest.approx([0.5])
        assert [interval.elapsed_hours for interval in intervals] == pytest.approx([0.5])
        assert diagnostics["midday_decrease"] == 1
        assert diagnostics["correction_reanchor"] == 1

    def test_cop_confirmation_rejects_transient_refresh_spike(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=10.8),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=30), heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=1), heat_kwh=10.4),
            SimpleNamespace(
                device_id="one", ts=base + dt.timedelta(hours=1, minutes=15), heat_kwh=10.4
            ),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("Europe/Stockholm"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert [(interval.energy_kwh, interval.elapsed_hours) for interval in intervals] == [
            (pytest.approx(0.4), pytest.approx(1.0))
        ]
        assert diagnostics == {
            "positive_pending": 2,
            "positive_reverted": 1,
            "positive_confirmed": 1,
        }

    def test_cop_confirmation_requires_a_later_reading_after_revert(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=10.8),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=30), heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=1), heat_kwh=10.3),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("Europe/Stockholm"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert intervals == []
        assert diagnostics == {
            "positive_pending": 2,
            "positive_reverted": 1,
            "positive_unresolved": 1,
        }

    def test_cop_confirmation_does_not_reuse_revert_history_after_zero_reads(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=10.8),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=30), heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=4), heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=8), heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=11), heat_kwh=10.3),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("Europe/Stockholm"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert intervals == []
        assert diagnostics == {
            "positive_pending": 2,
            "positive_reverted": 1,
            "zero_delta": 2,
            "positive_unresolved": 1,
        }

    def test_demand_default_emits_transient_refresh_spike_immediately(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=15), heat_kwh=10.8),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=30), heat_kwh=10.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=1), heat_kwh=10.4),
        ]

        intervals = list(iter_counter_change_intervals(rows, "heat_kwh", ZoneInfo("UTC")))

        assert [(interval.energy_kwh, interval.elapsed_hours) for interval in intervals] == [
            (pytest.approx(0.8), pytest.approx(0.25))
        ]

    @pytest.mark.parametrize(
        ("values", "expected_energy", "diagnostic"),
        [
            ([10.0, 10.8, 10.8], 0.8, "positive_confirmed"),
            ([10.0, 10.8, 10.4, 10.4], 0.4, "positive_replaced"),
        ],
    )
    def test_confirmation_positive_candidates(self, values, expected_energy, diagnostic):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15 * index),
                heat_kwh=value,
            )
            for index, value in enumerate(values)
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("UTC"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert [interval.energy_kwh for interval in intervals] == pytest.approx([expected_energy])
        assert diagnostics[diagnostic] == 1

    @pytest.mark.parametrize(
        ("values", "expected_energy", "diagnostic"),
        [
            ([10.0, 0.3, 0.4], 0.3, "reset_confirmed"),
            ([10.0, 0.3, 0.2, 0.2], 0.2, "reset_replaced"),
            ([10.0, 0.3, 10.1], None, "reset_abandoned"),
            ([10.0, 0.3], None, "reset_unresolved"),
        ],
    )
    def test_confirmation_reset_candidates(self, values, expected_energy, diagnostic):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 23, 50, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15 * index),
                heat_kwh=value,
            )
            for index, value in enumerate(values)
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("Europe/Stockholm"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert [interval.energy_kwh for interval in intervals] == (
            [] if expected_energy is None else pytest.approx([expected_energy])
        )
        assert diagnostics[diagnostic] == 1

    def test_explicit_equal_source_date_suppresses_timestamp_reset(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        rows = [
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime(2026, 1, 5, 23, 50, tzinfo=dt.timezone.utc),
                heat_kwh=10.0,
                source_date=dt.date(2026, 1, 5),
            ),
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime(2026, 1, 6, 0, 15, tzinfo=dt.timezone.utc),
                heat_kwh=0.3,
                source_date=dt.date(2026, 1, 5),
            ),
        ]
        diagnostics: dict[str, int] = {}

        assert (
            list(
                iter_counter_change_intervals(
                    rows, "heat_kwh", ZoneInfo("UTC"), diagnostics=diagnostics
                )
            )
            == []
        )
        assert diagnostics["midday_decrease"] == 1
        assert "source_day_reset" not in diagnostics

    def test_missing_or_mixed_source_date_uses_timestamp_compatibility(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 23, 50, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(
                device_id="one", ts=base, heat_kwh=10.0, source_date=dt.date(2026, 1, 5)
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=25),
                heat_kwh=0.3,
                source_date=None,
            ),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo("Europe/Stockholm"), diagnostics=diagnostics
            )
        )

        assert [interval.energy_kwh for interval in intervals] == pytest.approx([0.3])
        assert diagnostics["source_day_reset"] == 1

    def test_explicit_source_date_change_starts_and_confirms_reset(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 6, 0, 5, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(
                device_id="one", ts=base, heat_kwh=10.0, source_date=dt.date(2026, 1, 5)
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=10),
                heat_kwh=0.3,
                source_date=dt.date(2026, 1, 6),
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=20),
                heat_kwh=0.3,
                source_date=dt.date(2026, 1, 6),
            ),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("UTC"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert [interval.energy_kwh for interval in intervals] == pytest.approx([0.3])
        assert diagnostics["source_day_reset"] == 1
        assert diagnostics["reset_confirmed"] == 1

    def test_newer_source_day_replaces_pending_positive_with_reset(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 6, 0, 5, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(
                device_id="one", ts=base, heat_kwh=10.0, source_date=dt.date(2026, 1, 5)
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=10),
                heat_kwh=10.8,
                source_date=dt.date(2026, 1, 5),
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=20),
                heat_kwh=0.3,
                source_date=dt.date(2026, 1, 6),
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=30),
                heat_kwh=0.3,
                source_date=dt.date(2026, 1, 6),
            ),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("UTC"),
                diagnostics=diagnostics,
                confirm_changes=True,
            )
        )

        assert [interval.energy_kwh for interval in intervals] == pytest.approx([0.3])
        assert diagnostics["positive_abandoned"] == 1
        assert diagnostics["reset_confirmed"] == 1

    def test_irregular_steps_and_long_gaps_are_bounded_per_device(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        rows = [
            SimpleNamespace(device_id="one", ts=base, heat_kwh=1.0),
            SimpleNamespace(device_id="two", ts=base + dt.timedelta(minutes=15), heat_kwh=4.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(minutes=70), heat_kwh=1.5),
            SimpleNamespace(device_id="two", ts=base + dt.timedelta(minutes=95), heat_kwh=4.5),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=4), heat_kwh=2.0),
            SimpleNamespace(device_id="one", ts=base + dt.timedelta(hours=5), heat_kwh=2.5),
        ]
        diagnostics: dict[str, int] = {}
        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo("UTC"), diagnostics=diagnostics
            )
        )

        assert [item.device_id for item in intervals] == ["one", "two", "one"]
        assert [item.elapsed_hours for item in intervals] == pytest.approx([7 / 6, 4 / 3, 1.0])
        assert [item.energy_kwh for item in intervals] == pytest.approx([0.5, 0.5, 0.5])
        assert diagnostics["outside_window"] == 1

    @pytest.mark.parametrize(
        ("timezone", "start", "end", "expected_hours"),
        [
            (
                "Europe/Stockholm",
                dt.datetime(2026, 3, 29, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 3, 29, 3, tzinfo=dt.timezone.utc),
                3.0,
            ),
            (
                "Europe/Amsterdam",
                dt.datetime(2026, 10, 25, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 10, 25, 3, tzinfo=dt.timezone.utc),
                3.0,
            ),
        ],
    )
    def test_dst_uses_elapsed_instants(self, timezone, start, end, expected_hours):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        rows = [
            SimpleNamespace(device_id="one", ts=start, heat_kwh=1.0),
            SimpleNamespace(device_id="one", ts=end, heat_kwh=2.0),
        ]
        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo(timezone), max_interval_hours=4.0
            )
        )
        assert intervals[0].elapsed_hours == expected_hours

    def test_local_midnight_reset_uses_local_day_start(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        rows = [
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime(2026, 1, 5, 22, 50, tzinfo=dt.timezone.utc),
                heat_kwh=10.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime(2026, 1, 5, 23, 15, tzinfo=dt.timezone.utc),
                heat_kwh=0.3,
            ),
        ]
        intervals = list(
            iter_counter_change_intervals(rows, "heat_kwh", ZoneInfo("Europe/Amsterdam"))
        )
        assert intervals[0].energy_kwh == pytest.approx(0.3)
        assert intervals[0].elapsed_hours == pytest.approx(0.25)

    def test_utc_source_day_reset_uses_utc_boundary(self):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        rows = [
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime(2026, 1, 5, 23, 50, tzinfo=dt.timezone.utc),
                heat_kwh=10.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime(2026, 1, 6, 0, 15, tzinfo=dt.timezone.utc),
                heat_kwh=0.3,
            ),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows, "heat_kwh", ZoneInfo("Europe/Stockholm"), diagnostics=diagnostics
            )
        )

        assert intervals[0].energy_kwh == pytest.approx(0.3)
        assert intervals[0].elapsed_hours == pytest.approx(0.25)
        assert diagnostics["source_day_reset"] == 1

    @pytest.mark.parametrize(
        "reset_day",
        [dt.date(2026, 3, 29), dt.date(2026, 10, 25)],
    )
    def test_dst_day_uses_utc_reset_even_when_local_date_is_unchanged(self, reset_day):
        from zoneinfo import ZoneInfo

        from packages.ml.models_common import iter_counter_change_intervals

        previous_day = reset_day - dt.timedelta(days=1)
        rows = [
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime.combine(previous_day, dt.time(23, 50), tzinfo=dt.timezone.utc),
                heat_kwh=10.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=dt.datetime.combine(reset_day, dt.time(0, 15), tzinfo=dt.timezone.utc),
                heat_kwh=0.3,
            ),
        ]
        diagnostics: dict[str, int] = {}

        intervals = list(
            iter_counter_change_intervals(
                rows,
                "heat_kwh",
                ZoneInfo("Europe/Stockholm"),
                diagnostics=diagnostics,
                max_interval_hours=4.0,
            )
        )

        assert len(intervals) == 1
        assert intervals[0].energy_kwh == pytest.approx(0.3)
        assert intervals[0].elapsed_hours == pytest.approx(0.25)
        assert diagnostics["source_day_reset"] == 1
        assert diagnostics.get("local_day_reset", 0) == 0

    @pytest.mark.asyncio
    async def test_demand_accepts_four_hour_counter_interval_but_caps_longer_gaps(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one", ts=base, heat_kwh=0.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=2.0
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(hours=4),
                heat_kwh=2.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(hours=8, minutes=1),
                heat_kwh=2.5,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(hours=8, minutes=16),
                heat_kwh=3.0,
                cool_kwh=0.0,
                tank_kwh=0.0,
                outdoor_temp=2.0,
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]
        model = DemandModel()

        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session([_FakeResult(consumption), _FakeResult(weather)]),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            _, target = await model._prepare_data()

        assert target.tolist() == pytest.approx([0.5, 2.0])
        assert model.last_data_quality["counter_outside_window"] == 1


class TestThermalModel:
    def test_default_params(self):
        """Model should have sensible default params."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        assert model.params.tank_heating_rate > 0
        assert model.params.tank_standby_loss < 0
        assert model.params.zone_heating_rate > 0

    def test_predict_tank_heating_time(self):
        """Should predict positive heating time for a temperature increase."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        pred = model.predict_tank_heating_time(
            current_temp=45.0, target_temp=52.0, outdoor_temp=5.0
        )

        assert pred.estimated_minutes > 0
        assert pred.heating_rate_per_hour > 0
        assert pred.confidence == "default"

    def test_predict_tank_already_at_target(self):
        """Should return 0 minutes if already at target."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        pred = model.predict_tank_heating_time(
            current_temp=55.0, target_temp=52.0, outdoor_temp=5.0
        )

        assert pred.estimated_minutes == 0.0

    def test_predict_tank_cooling_time(self):
        """Should predict positive cooling time from above minimum."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        pred = model.predict_tank_cooling_time(current_temp=52.0, min_temp=45.0, outdoor_temp=5.0)

        assert pred.estimated_minutes > 0

    def test_predict_zone_heating_time(self):
        """Zone heating prediction should be positive for a delta."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        pred = model.predict_zone_heating_time(
            current_temp=18.0, target_temp=22.0, outdoor_temp=5.0
        )

        assert pred.estimated_minutes > 0

    def test_optimal_start_time(self):
        """Optimal start should be before the deadline."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        deadline = dt.datetime(2026, 5, 1, 6, 0, tzinfo=dt.timezone.utc)
        start = model.optimal_start_time(
            current_temp=45.0,
            target_temp=52.0,
            deadline=deadline,
            outdoor_temp=5.0,
            is_tank=True,
        )

        assert start < deadline

    def test_warmer_outdoor_faster_heating(self):
        """Warmer outdoor temp should give faster (shorter) heating."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()

        cold = model.predict_tank_heating_time(45.0, 52.0, outdoor_temp=-5.0)
        warm = model.predict_tank_heating_time(45.0, 52.0, outdoor_temp=15.0)

        assert warm.estimated_minutes < cold.estimated_minutes

    def test_temperature_curve(self):
        """Temperature curve should have correct length."""
        from packages.ml.thermal import ThermalModel

        model = ThermalModel()
        curve = model.predict_temperature_curve(
            current_temp=52.0, outdoor_temp=5.0, hours=12, is_tank=True
        )

        assert len(curve) == 12
        # Standby loss: temperatures should decrease
        assert curve[-1]["predicted_temp"] < 52.0


class TestAssumedHeatingEfficiency:
    @pytest.mark.parametrize(
        ("temperature", "expected"),
        [
            (-100.0, 1.5),
            (-20.0, 1.5),
            (-5.0, 3.0),
            (0.0, 3.5),
            (5.0, 4.0),
            (25.0, 6.0),
            (100.0, 6.0),
        ],
    )
    def test_original_bounded_curve_is_only_a_planning_assumption(self, temperature, expected):
        from packages.ml.thermal import assumed_heating_efficiency

        assert assumed_heating_efficiency(temperature) == expected
        assert assumed_heating_efficiency(temperature) == max(
            1.5, min(6.0, 3.5 + 0.1 * temperature)
        )


class TestMonotonicDemandModel:
    """Demand must rise as it gets colder even when trained on bad data."""

    @pytest.mark.asyncio
    async def test_demand_non_increasing_in_outdoor_on_inverted_data(self):
        from packages.ml.demand_model_core import DemandModel

        rng = np.random.RandomState(0)
        n = 300
        outdoor = rng.uniform(-10, 20, n)
        wind = rng.uniform(0, 10, n)
        irradiance = rng.uniform(0, 500, n)
        hours = rng.randint(0, 24, n)
        dow = rng.randint(0, 7, n)
        X = np.column_stack(
            [
                outdoor,
                wind,
                irradiance,
                np.zeros(n),
                np.full(n, 60.0),
                np.full(n, 0.5),
                np.sin(2 * np.pi * hours / 24),
                np.cos(2 * np.pi * hours / 24),
                np.sin(2 * np.pi * dow / 7),
                np.cos(2 * np.pi * dow / 7),
            ]
        )
        # Physically wrong: warmer outside -> higher demand.
        y = np.clip(1.0 + 0.1 * outdoor + rng.normal(0, 0.1, n), 0.1, None)

        model = DemandModel()

        async def fake_prep():
            return X, y

        model._prepare_data = fake_prep
        with patch("packages.ml.safe_persistence.safe_dump"):
            refused = await model.train()
        # Physically inverted data has no skill once monotonicity is enforced.
        assert refused["status"] == "no_skill"
        assert model.is_trained is False

        with (
            patch("packages.ml.safe_persistence.safe_dump") as publish,
            patch("packages.ml.demand_model_core.time_series_cv_naive_mae", return_value=100.0),
        ):
            result = await model.train()
        assert result["status"] == "maintenance_required"
        assert model.is_trained is False
        publish.assert_not_called()
        fitted = model._make_quantile_model(0.5)
        fitted.fit(X[:240], y[:240])

        f_warm = DemandModel._make_features(15.0, 3.0, 0.0, 12, 2).reshape(1, -1)
        f_cold = DemandModel._make_features(-5.0, 3.0, 0.0, 12, 2).reshape(1, -1)
        warm = float(fitted.predict(f_warm)[0])
        cold = float(fitted.predict(f_cold)[0])
        assert cold >= warm - 1e-6


class TestPhysicalCurveOrdering:
    """The indoor-forecast endpoint must never show heating below no-heating."""

    def test_predicted_clamped_to_no_heating_floor(self):
        from packages.api.routers.models_router import _enforce_physical_ordering

        # Reproduces the reported inversion: predicted 25.9 < no-heating 26.2.
        forecast = [{"predicted_indoor_temp": 25.9}]
        forecast_with_plan = [{"predicted_indoor_temp": 25.0}]
        forecast_no_heating = [{"predicted_indoor_temp": 26.2}]

        _enforce_physical_ordering(forecast, forecast_with_plan, forecast_no_heating)

        assert forecast[0]["predicted_indoor_temp"] == 26.2
        assert forecast_with_plan[0]["predicted_indoor_temp"] >= 26.2

    def test_valid_ordering_left_untouched(self):
        from packages.api.routers.models_router import _enforce_physical_ordering

        forecast = [{"predicted_indoor_temp": 22.0}]
        forecast_with_plan = [{"predicted_indoor_temp": 23.0}]
        forecast_no_heating = [{"predicted_indoor_temp": 20.0}]

        _enforce_physical_ordering(forecast, forecast_with_plan, forecast_no_heating)

        assert forecast[0]["predicted_indoor_temp"] == 22.0
        assert forecast_with_plan[0]["predicted_indoor_temp"] == 23.0


class TestOrchestratorFallback:
    """Tests for the orchestrator layer selection and fallback logic."""

    @pytest.fixture(autouse=True)
    def _learning_gate(self, optimization_learning_gate):
        return optimization_learning_gate

    @pytest.mark.asyncio
    async def test_select_optimizer_can_reload_models(self):
        """reload_models=True should refresh checkpoints before selecting a layer."""
        from packages.optimizer.main import _select_optimizer

        with patch("packages.optimizer.main._load_ml_models") as mock_load:
            layer_name, optimizer = await _select_optimizer("rules_only", reload_models=True)

        mock_load.assert_called_once_with()
        assert layer_name == "rules"
        assert type(optimizer).__name__ == "RulesOptimizer"

    @pytest.mark.asyncio
    async def test_rules_only_never_uses_milp(self):
        """With rules_only setting, MILP should never be invoked."""
        from packages.optimizer.main import _select_optimizer

        layer_name, optimizer = await _select_optimizer("rules_only")
        assert layer_name == "rules"
        assert type(optimizer).__name__ == "RulesOptimizer"

    @pytest.mark.asyncio
    async def test_milp_preferred_returns_milp(self):
        """milp_preferred should return MILP optimizer."""
        from packages.optimizer.main import _select_optimizer

        layer_name, optimizer = await _select_optimizer("milp_preferred")
        assert layer_name == "milp"
        assert type(optimizer).__name__ == "MILPOptimizer"

    @pytest.mark.asyncio
    async def test_auto_uses_rules_without_ml(self):
        """auto remains on rules without a qualification query."""
        from packages.optimizer.main import _select_optimizer

        with patch("packages.optimizer.main.get_session", side_effect=AssertionError("auto query")):
            layer_name, optimizer = await _select_optimizer("auto")
        assert layer_name == "rules"
        assert type(optimizer).__name__ == "RulesOptimizer"

    @pytest.mark.parametrize("trained", [False, True])
    async def test_auto_ignores_legacy_artifacts_and_never_queries_history(self, trained, tmp_path):
        from packages.optimizer import main as optimizer_main

        artifact = tmp_path / "cop_model_weather_dhw_v6_legacy.pkl"
        artifact.write_bytes(b"legacy trained COP")
        demand = MagicMock(is_trained=trained)
        with (
            patch.object(optimizer_main, "_demand_model", demand),
            patch.object(optimizer_main.comfort_model, "load_latest"),
            patch.object(optimizer_main.thermal_model, "load_latest"),
            patch.object(optimizer_main, "get_session", side_effect=AssertionError("auto query")),
            patch.object(optimizer_main, "MILPOptimizer", side_effect=AssertionError("auto MILP")),
            patch(
                "packages.ml.models.COPModel.load_latest", side_effect=AssertionError("COP load")
            ),
        ):
            layer_name, optimizer = await optimizer_main._select_optimizer(
                "auto", reload_models=True
            )

        assert layer_name == "rules"
        assert type(optimizer).__name__ == "RulesOptimizer"
        demand.load_latest.assert_called_once_with()
        assert artifact.read_bytes() == b"legacy trained COP"

    @pytest.mark.parametrize("trained", [False, True])
    async def test_optimizer_status_reports_retirement_and_rules_for_auto(self, trained):
        from packages.optimizer import main as optimizer_main

        with (
            patch.object(optimizer_main, "_demand_model", MagicMock(is_trained=trained)),
            patch.object(
                optimizer_main,
                "get_planning_data_quality",
                new=AsyncMock(return_value={"control_allowed": True}),
            ),
            patch.object(optimizer_main, "get_session", side_effect=AssertionError("auto query")),
        ):
            status = await optimizer_main.get_optimizer_status_snapshot("auto")

        assert status["active_layer"] == "rules_v7"
        assert status["configured_layer"] == "auto"
        assert status["effective_layer"] == "rules"
        assert status["selection_reason"] == "auto_milp_disabled_cop_retired"
        assert status["cop_trained"] is False
        assert status["cop_source"] == "assumed"
        assert status["cop_measured"] is False
        assert status["demand_trained"] is trained

    @pytest.mark.parametrize("trained", [False, True])
    async def test_explicit_milp_only_receives_retained_demand(self, trained):
        from packages.optimizer import main as optimizer_main

        demand = MagicMock(is_trained=trained)
        with (
            patch.object(optimizer_main, "_demand_model", demand),
            patch.object(optimizer_main, "MILPOptimizer") as milp,
        ):
            layer_name, optimizer = await optimizer_main._select_optimizer("milp_preferred")
            version = optimizer_main._selected_layer_version("milp")

        assert layer_name == "milp"
        assert optimizer is milp.return_value
        milp.assert_called_once_with(demand_model=demand if trained else None)
        assert version == (f"{milp.VERSION}+ml" if trained else milp.VERSION)

    @pytest.mark.asyncio
    async def test_run_optimization_with_rules_only(self):
        """End-to-end: rules_only setting should produce a rules plan."""
        from packages.optimizer.main import run_optimization

        with patch(
            "packages.optimizer.main.get_setting", new_callable=AsyncMock, return_value="rules_only"
        ):
            with patch("packages.optimizer.main.RulesOptimizer") as MockRules:
                mock_plan = {
                    "horizon_start": dt.datetime.now(dt.timezone.utc),
                    "horizon_end": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24),
                    "actions": [],
                    "version": "rules_v3",
                    "cost_estimate": 0.0,
                }
                MockRules.return_value.generate_plan = AsyncMock(return_value=mock_plan)

                with (
                    patch(
                        "packages.optimizer.main.get_session",
                        _mock_get_session([_FakeResult([]), _FakeResult([]), _FakeResult([])]),
                    ),
                    patch(
                        "packages.optimizer.main.get_active_price_context",
                        AsyncMock(
                            return_value=SimpleNamespace(area="NL", currency="EUR", source="test")
                        ),
                    ),
                    patch(
                        "packages.optimizer.main.get_device_data_quality",
                        AsyncMock(return_value={"ready": True, "reasons": []}),
                    ),
                ):
                    await run_optimization()

                MockRules.return_value.generate_plan.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure", [SolverTimeoutError("timeout"), InfeasibleError("infeasible")]
    )
    async def test_milp_failure_falls_back_to_rules(self, failure):
        """When MILP raises, the orchestrator should fall back to rules."""
        from packages.optimizer.main import run_optimization

        mock_plan = {
            "horizon_start": dt.datetime.now(dt.timezone.utc),
            "horizon_end": dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24),
            "actions": [],
            "version": "rules_v3",
            "cost_estimate": 0.0,
        }

        with patch(
            "packages.optimizer.main.get_setting",
            new_callable=AsyncMock,
            return_value="milp_preferred",
        ):
            with patch(
                "packages.optimizer.main._select_optimizer", new_callable=AsyncMock
            ) as mock_select:
                mock_milp = AsyncMock()
                mock_milp.generate_plan = AsyncMock(side_effect=failure)
                mock_select.return_value = ("milp", mock_milp)

                with patch("packages.optimizer.main.RulesOptimizer") as MockRules:
                    MockRules.return_value.generate_plan = AsyncMock(return_value=mock_plan)

                    with (
                        patch(
                            "packages.optimizer.main.get_session",
                            _mock_get_session([_FakeResult([]), _FakeResult([])]),
                        ),
                        patch(
                            "packages.optimizer.main.get_planning_data_quality",
                            AsyncMock(
                                return_value={
                                    "control_allowed": True,
                                    "status": "ready",
                                    "reasons": [],
                                    "price": {},
                                    "weather": {},
                                }
                            ),
                        ),
                        patch(
                            "packages.optimizer.main.get_active_price_context",
                            AsyncMock(
                                return_value=SimpleNamespace(
                                    area="NL", currency="EUR", source="test"
                                )
                            ),
                        ),
                        patch(
                            "packages.optimizer.main.get_device_data_quality",
                            AsyncMock(return_value={"ready": True, "reasons": []}),
                        ),
                    ):
                        await run_optimization()

                    # MILP failed, so rules should have been called
                    MockRules.return_value.generate_plan.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_planning_quality_check_failure_blocks_plan(self):
        """An unavailable planning-quality gate must not permit a rules fallback."""
        from packages.optimizer.main import run_optimization

        optimizer = AsyncMock()
        with (
            patch(
                "packages.optimizer.main.get_setting",
                new=AsyncMock(return_value="milp_preferred"),
            ),
            patch(
                "packages.optimizer.main._select_optimizer",
                new=AsyncMock(return_value=("milp", optimizer)),
            ),
            patch(
                "packages.optimizer.main.comfort_model",
                SimpleNamespace(arefresh_if_changed=AsyncMock()),
            ),
            patch(
                "packages.optimizer.main.get_device_data_quality",
                new=AsyncMock(return_value={"ready": True, "reasons": []}),
            ),
            patch(
                "packages.optimizer.main.get_planning_data_quality",
                new=AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
            patch("packages.optimizer.main.logger") as logger,
        ):
            assert await run_optimization() is None

        optimizer.generate_plan.assert_not_awaited()
        logger.error.assert_called_once_with(
            "optimization_paused_planning_data_quality_check_failed",
            reason="quality_check_failed",
            error_type="RuntimeError",
        )

    @pytest.mark.asyncio
    async def test_milp_failure_fallback_remains_behind_device_gate(self):
        """A failed MILP cannot reach its rules fallback when readiness is unavailable."""
        from packages.optimizer.main import run_optimization

        milp = AsyncMock()
        with (
            patch(
                "packages.optimizer.main.get_setting",
                new=AsyncMock(return_value="milp_preferred"),
            ),
            patch(
                "packages.optimizer.main._select_optimizer",
                new=AsyncMock(return_value=("milp", milp)),
            ),
            patch(
                "packages.optimizer.main.comfort_model",
                SimpleNamespace(arefresh_if_changed=AsyncMock()),
            ),
            patch(
                "packages.optimizer.main.get_device_data_quality",
                new=AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
            patch("packages.optimizer.main.RulesOptimizer") as rules,
        ):
            assert await run_optimization() is None

        milp.generate_plan.assert_not_awaited()
        rules.return_value.generate_plan.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_learning_state_skips_before_optimizer_selection(
        self, optimization_learning_gate
    ):
        from packages.optimizer.executor_core import LearningModeState
        from packages.optimizer.main import run_optimization

        optimization_learning_gate.return_value = LearningModeState.UNKNOWN
        with patch(
            "packages.optimizer.main._select_optimizer", new=AsyncMock()
        ) as select_optimizer:
            assert await run_optimization() is None
        select_optimizer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_learning_state_lookup_failure_skips_before_optimizer_selection(
        self, optimization_learning_gate
    ):
        from packages.optimizer.main import run_optimization

        optimization_learning_gate.side_effect = RuntimeError("settings unavailable")
        with patch(
            "packages.optimizer.main._select_optimizer", new=AsyncMock()
        ) as select_optimizer:
            assert await run_optimization() is None
        select_optimizer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_learning_state_does_not_touch_active_plan_or_safety_links(
        self, optimization_learning_gate
    ):
        from packages.optimizer.executor_core import LearningModeState
        from packages.optimizer.main import run_optimization

        optimization_learning_gate.return_value = LearningModeState.UNKNOWN
        with (
            patch("packages.optimizer.main.get_session") as get_session,
            patch("packages.optimizer.main._select_optimizer", new=AsyncMock()) as select_optimizer,
            patch(
                "packages.optimizer.main.comfort_model",
                SimpleNamespace(arefresh_if_changed=AsyncMock()),
            ) as comfort_model,
        ):
            assert await run_optimization(force_replace=True) is None

        # The active plan and any pending linked safety restore remain untouched
        # because UNKNOWN returns before opening the lifecycle transaction.
        get_session.assert_not_called()
        select_optimizer.assert_not_awaited()
        comfort_model.arefresh_if_changed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_learning_state_cancellation_propagates(self, optimization_learning_gate):
        from packages.optimizer.main import run_optimization

        optimization_learning_gate.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await run_optimization()

    @pytest.mark.asyncio
    async def test_active_learning_state_reaches_normal_admission_checks(
        self, optimization_learning_gate
    ):
        from packages.optimizer.executor_core import LearningModeState
        from packages.optimizer.main import run_optimization

        optimization_learning_gate.return_value = LearningModeState.ACTIVE
        optimizer = AsyncMock()
        with (
            patch(
                "packages.optimizer.main._select_optimizer",
                new=AsyncMock(return_value=("rules", optimizer)),
            ) as select_optimizer,
            patch("packages.optimizer.main.get_setting", new=AsyncMock(return_value="rules_only")),
            patch(
                "packages.optimizer.main.comfort_model",
                SimpleNamespace(arefresh_if_changed=AsyncMock()),
            ),
            patch(
                "packages.optimizer.main.get_device_data_quality",
                new=AsyncMock(return_value={"ready": False, "reasons": ["credentials_missing"]}),
            ),
        ):
            assert await run_optimization() is None
        select_optimizer.assert_awaited_once()
        optimizer.generate_plan.assert_not_awaited()


def test_model_dir_is_isolated_to_pytest_tmp(isolate_model_dir: Path) -> None:
    """Unit tests must never resolve model artifacts through the production directory."""
    from packages.core.config import settings
    from packages.ml import comfort_model, models_common, thermal

    expected = isolate_model_dir.resolve()
    assert Path(settings.model_dir).resolve() == expected
    assert models_common.MODEL_DIR.resolve() == expected
    assert comfort_model.MODEL_DIR.resolve() == expected
    assert thermal.MODEL_DIR.resolve() == expected


class TestDemandHeatingOnlyTarget:
    @pytest.mark.asyncio
    async def test_dhw_only_interval_is_skipped(self):
        from packages.ml.models import DemandModel

        base = dt.datetime(2026, 1, 5, 8, 0, tzinfo=dt.timezone.utc)
        consumption = [
            SimpleNamespace(
                device_id="one", ts=base, heat_kwh=0.0, cool_kwh=0.0, tank_kwh=0.0, outdoor_temp=2.0
            ),
            SimpleNamespace(
                device_id="one",
                ts=base + dt.timedelta(minutes=15),
                heat_kwh=0.0,
                cool_kwh=0.0,
                tank_kwh=1.0,
                outdoor_temp=2.0,
            ),
        ]
        weather = [SimpleNamespace(ts=base, temperature=1.0, wind_speed=4.0, irradiance=0.0)]

        model = DemandModel()
        with (
            patch(
                "packages.ml.demand_model_core.get_session",
                _mock_get_session([_FakeResult(consumption), _FakeResult(weather)]),
            ),
            patch("packages.ml.demand_model_core.get_user_tz", new=AsyncMock(return_value="UTC")),
        ):
            _, target = await model._prepare_data()

        assert len(target) == 0


class TestDemandQuantiles:
    def test_untrained_band_is_degenerate(self):
        from packages.ml.models import DemandModel

        band = DemandModel().predict_hourly_quantiles([{"temperature": 5.0}] * 3, hours=3)

        assert len(band) == 3
        assert all(entry["p10"] == entry["p50"] == entry["p90"] >= 0 for entry in band)


class TestMAEBaseline:
    def test_write_atomically_replaces_baseline(self, tmp_path):
        from packages.ml import models_common as models_common

        baseline_path = tmp_path / "cop_mae_baseline.json"
        baseline_path.write_text('{"mae": 1.0}')
        with (
            patch.object(models_common, "MODEL_DIR", tmp_path),
            patch.object(models_common.os, "replace", wraps=models_common.os.replace) as replace,
        ):
            models_common.write_mae_baseline("cop", 0.5)
            assert models_common.read_mae_baseline("cop") == 0.5

        replace.assert_called_once()
        temporary_path, destination_path = replace.call_args.args
        assert Path(temporary_path).parent == tmp_path
        assert destination_path == baseline_path
        assert set(json.loads(baseline_path.read_text())) == {"mae", "updated_at"}

    def test_write_failure_preserves_baseline_and_removes_temp_file(self, tmp_path):
        from packages.ml import models_common as models_common

        baseline_path = tmp_path / "cop_mae_baseline.json"
        original_contents = '{"mae": 1.0}'
        baseline_path.write_text(original_contents)
        with (
            patch.object(models_common, "MODEL_DIR", tmp_path),
            patch.object(models_common.os, "replace", side_effect=OSError("replace failed")),
        ):
            models_common.write_mae_baseline("cop", 0.5)

        assert baseline_path.read_text() == original_contents
        assert not list(tmp_path.glob(".cop_mae_baseline.json.*.tmp"))

    @pytest.mark.parametrize("contents", [None, "not json", '{"mae": null}', "{}"])
    def test_read_missing_or_malformed_baseline_returns_none(self, tmp_path, contents):
        from packages.ml import models_common as models_common

        baseline_path = tmp_path / "cop_mae_baseline.json"
        if contents is not None:
            baseline_path.write_text(contents)

        with patch.object(models_common, "MODEL_DIR", tmp_path):
            assert models_common.read_mae_baseline("cop") is None

    def test_regression_blocks_deploy(self, tmp_path):
        from packages.ml import models_common as models_common

        with patch.object(models_common, "MODEL_DIR", tmp_path):
            models_common.write_mae_baseline("cop", 0.5)
            decision = models_common.evaluate_regression("cop", mae=1.0, has_prior_model=True)

        assert decision["deploy"] is False
        assert decision["improved"] is False


class TestModelCheckpointRetention:
    def test_keeps_only_newest_matching_checkpoints(self, tmp_path):
        from packages.ml.models_common import prune_old_models

        for index in range(7):
            (tmp_path / f"cop_model_{index:02d}.pkl").write_bytes(b"model")
        unrelated = tmp_path / "thermal_params_v1.pkl"
        unrelated.write_bytes(b"thermal")

        assert prune_old_models("cop_model_*.pkl", keep=5, model_dir=tmp_path) == 2
        assert [path.name for path in sorted(tmp_path.glob("cop_model_*.pkl"))] == [
            f"cop_model_{index:02d}.pkl" for index in range(2, 7)
        ]
        assert unrelated.exists()

    def test_rejects_negative_retention(self, tmp_path):
        from packages.ml.models_common import prune_old_models

        with pytest.raises(ValueError, match="keep must be >= 0"):
            prune_old_models("*.pkl", keep=-1, model_dir=tmp_path)
