from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from packages.api.routers import settings


@pytest.mark.asyncio
async def test_update_settings_returns_bad_request_for_bulk_validation_error():
    with patch.object(
        settings, "set_settings_bulk", new=AsyncMock(side_effect=ValueError("invalid batch"))
    ):
        with pytest.raises(HTTPException) as exc_info:
            await settings.update_settings(
                settings.SettingsUpdate(settings={"price_provider": "manual"})
            )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "invalid batch"


@pytest.mark.asyncio
async def test_update_settings_rejects_hidden_key_without_writing():
    with patch.object(settings, "set_settings_bulk", new=AsyncMock()) as write:
        with pytest.raises(HTTPException) as exc_info:
            await settings.update_settings(
                settings.SettingsUpdate(settings={"_heat_curve_verification_state": "{}"})
            )

    assert exc_info.value.status_code == 400
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_settings_rejects_hidden_key_before_mixed_bulk_write():
    with patch.object(settings, "set_settings_bulk", new=AsyncMock()) as write:
        with pytest.raises(HTTPException) as exc_info:
            await settings.update_settings(
                settings.SettingsUpdate(
                    settings={
                        "price_provider": "manual",
                        "_seasonal_calibration_safety_deferred_since": "now",
                    }
                )
            )

    assert exc_info.value.status_code == 400
    write.assert_not_awaited()


class TestBaselinePromotionSettings:
    @staticmethod
    def _session_context():
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=MagicMock(add=MagicMock()))
        context.__aexit__ = AsyncMock(return_value=False)
        return context

    @pytest.mark.asyncio
    async def test_invalid_fraction_rejects_entire_bulk_put(self):
        with patch.object(settings, "set_settings_bulk", new=AsyncMock()) as write:
            with pytest.raises(HTTPException) as exc_info:
                await settings.update_settings(
                    settings.SettingsUpdate(
                        settings={
                            "price_provider": "manual",
                            "space_heating_default_fraction": "nan",
                        }
                    )
                )

        assert exc_info.value.status_code == 400
        write.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("current", ["off", "shadow"])
    async def test_non_on_to_on_not_ready_returns_409_without_writes(self, current):
        with (
            patch.object(
                settings,
                "get_all_settings",
                new=AsyncMock(return_value={"space_heating_baseline_mode": current}),
            ),
            patch.object(
                settings, "get_baseline_promotion_readiness", new=AsyncMock(return_value=False)
            ),
            patch.object(settings, "set_settings_bulk", new=AsyncMock()) as write,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await settings.update_settings(
                    settings.SettingsUpdate(settings={"space_heating_baseline_mode": "on"})
                )

        assert exc_info.value.status_code == 409
        write.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("current", "requested"),
        [("off", "on"), ("shadow", "on"), ("on", "off"), ("on", "shadow")],
    )
    async def test_ready_entry_paths_succeed_and_rollbacks_always_succeed(self, current, requested):
        readiness = AsyncMock(return_value=True)
        with (
            patch.object(
                settings,
                "get_all_settings",
                new=AsyncMock(return_value={"space_heating_baseline_mode": current}),
            ),
            patch.object(settings, "get_baseline_promotion_readiness", new=readiness),
            patch.object(settings, "set_settings_bulk", new=AsyncMock()) as write,
            patch.object(settings, "get_session", return_value=self._session_context()),
        ):
            response = await settings.update_settings(
                settings.SettingsUpdate(settings={"space_heating_baseline_mode": requested})
            )

        assert response["status"] == "updated"
        write.assert_awaited_once_with({"space_heating_baseline_mode": requested})
        if current == "on":
            readiness.assert_not_awaited()
        else:
            readiness.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_repeated_on_to_on_does_not_require_scorecard(self):
        readiness = AsyncMock(side_effect=AssertionError("scorecard should not be read"))
        with (
            patch.object(
                settings,
                "get_all_settings",
                new=AsyncMock(return_value={"space_heating_baseline_mode": "on"}),
            ),
            patch.object(settings, "get_baseline_promotion_readiness", new=readiness),
            patch.object(settings, "set_settings_bulk", new=AsyncMock()) as write,
            patch.object(settings, "get_session", return_value=self._session_context()),
        ):
            await settings.update_settings(
                settings.SettingsUpdate(settings={"space_heating_baseline_mode": "on"})
            )

        readiness.assert_not_awaited()
        write.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("current", ["off", "shadow"])
    async def test_ready_scorer_allows_off_or_shadow_to_on(self, current):
        with (
            patch.object(
                settings,
                "get_all_settings",
                new=AsyncMock(return_value={"space_heating_baseline_mode": current}),
            ),
            patch.object(
                settings, "get_baseline_promotion_readiness", new=AsyncMock(return_value=True)
            ),
            patch.object(settings, "set_settings_bulk", new=AsyncMock()) as write,
            patch.object(settings, "get_session", return_value=self._session_context()),
        ):
            response = await settings.update_settings(
                settings.SettingsUpdate(settings={"space_heating_baseline_mode": "on"})
            )

        assert response["status"] == "updated"
        write.assert_awaited_once_with({"space_heating_baseline_mode": "on"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("requested", ["shadow", "off"])
    async def test_rollbacks_pass_even_when_promotion_scorer_raises(self, requested):
        with (
            patch.object(
                settings,
                "get_all_settings",
                new=AsyncMock(return_value={"space_heating_baseline_mode": "on"}),
            ),
            patch.object(
                settings,
                "get_baseline_promotion_readiness",
                new=AsyncMock(side_effect=RuntimeError("scorecard unavailable")),
            ) as readiness,
            patch.object(settings, "set_settings_bulk", new=AsyncMock()) as write,
            patch.object(settings, "get_session", return_value=self._session_context()),
        ):
            response = await settings.update_settings(
                settings.SettingsUpdate(settings={"space_heating_baseline_mode": requested})
            )

        assert response["status"] == "updated"
        readiness.assert_not_awaited()
        write.assert_awaited_once_with({"space_heating_baseline_mode": requested})
