"""Tests for solar-geometry irradiance estimation."""

import datetime as dt

from packages.core.solar import clear_sky_ghi, estimate_ghi, solar_elevation_deg


class TestSolarElevation:
    def test_sun_below_horizon_at_night(self):
        # Midnight UTC near Stockholm in winter → sun well below the horizon.
        ts = dt.datetime(2026, 1, 1, 0, tzinfo=dt.timezone.utc)
        assert solar_elevation_deg(59.33, 18.06, ts) < 0

    def test_sun_high_at_solar_noon_equator_equinox(self):
        # Solar noon at the equator on the equinox → sun nearly overhead.
        ts = dt.datetime(2026, 3, 20, 12, tzinfo=dt.timezone.utc)
        assert solar_elevation_deg(0.0, 0.0, ts) > 80


class TestClearSkyGhi:
    def test_zero_at_night(self):
        ts = dt.datetime(2026, 1, 1, 0, tzinfo=dt.timezone.utc)
        assert clear_sky_ghi(59.33, 18.06, ts) == 0.0

    def test_high_at_solar_noon(self):
        ts = dt.datetime(2026, 3, 20, 12, tzinfo=dt.timezone.utc)
        assert clear_sky_ghi(0.0, 0.0, ts) > 500

    def test_naive_datetime_treated_as_utc(self):
        aware = dt.datetime(2026, 6, 21, 12, tzinfo=dt.timezone.utc)
        naive = dt.datetime(2026, 6, 21, 12)
        assert clear_sky_ghi(55.0, 12.0, naive) == clear_sky_ghi(55.0, 12.0, aware)


class TestEstimateGhi:
    def test_clouds_reduce_irradiance(self):
        ts = dt.datetime(2026, 6, 21, 12, tzinfo=dt.timezone.utc)
        clear = estimate_ghi(55.0, 12.0, ts, cloud_fraction=0.0)
        partial = estimate_ghi(55.0, 12.0, ts, cloud_fraction=0.5)
        overcast = estimate_ghi(55.0, 12.0, ts, cloud_fraction=1.0)
        assert clear > partial > overcast > 0

    def test_zero_at_night_regardless_of_clouds(self):
        ts = dt.datetime(2026, 1, 1, 0, tzinfo=dt.timezone.utc)
        assert estimate_ghi(59.33, 18.06, ts, cloud_fraction=0.0) == 0.0

    def test_cloud_fraction_clamped(self):
        ts = dt.datetime(2026, 6, 21, 12, tzinfo=dt.timezone.utc)
        # Out-of-range values must not blow up or change sign.
        assert estimate_ghi(55.0, 12.0, ts, cloud_fraction=-1.0) == estimate_ghi(
            55.0, 12.0, ts, cloud_fraction=0.0
        )
        assert estimate_ghi(55.0, 12.0, ts, cloud_fraction=2.0) == estimate_ghi(
            55.0, 12.0, ts, cloud_fraction=1.0
        )
