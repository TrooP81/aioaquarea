"""Solar geometry helpers: estimate surface irradiance from location and time.

Some weather providers (notably SMHI) do not publish a solar-radiation
parameter, but they do publish cloud cover. This module reconstructs a
physically plausible global horizontal irradiance (GHI, W/m²) from:

1. The sun's elevation above the horizon (from latitude, longitude and UTC
   time) → a clear-sky GHI via the Haurwitz model.
2. Cloud cover attenuation via the Kasten–Czeplak relation.

The result feeds the same ``irradiance`` field the comfort/thermal models
already consume, so temperature predictions reflect sunny vs. overcast hours
without any schema or model-feature changes.
"""

from __future__ import annotations

import datetime as dt
import math

# Solar constant scaling for the Haurwitz clear-sky model (W/m²).
_HAURWITZ_A = 1098.0
_HAURWITZ_B = 0.059


def solar_elevation_deg(latitude: float, longitude: float, ts: dt.datetime) -> float:
    """Return the sun's elevation angle (degrees) above the horizon.

    Negative values mean the sun is below the horizon (night). ``ts`` must be a
    timezone-aware UTC datetime.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    ts = ts.astimezone(dt.timezone.utc)

    day_of_year = ts.timetuple().tm_yday
    utc_hours = ts.hour + ts.minute / 60.0 + ts.second / 3600.0

    # Solar declination (deg).
    decl = 23.45 * math.sin(math.radians(360.0 * (284 + day_of_year) / 365.0))

    # Equation of time (minutes) — corrects for Earth's orbital eccentricity.
    b = math.radians(360.0 * (day_of_year - 81) / 364.0)
    eot = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)

    # Local solar time and hour angle (deg), 0° at solar noon.
    solar_time = utc_hours + longitude / 15.0 + eot / 60.0
    hour_angle = 15.0 * (solar_time - 12.0)

    lat_r = math.radians(latitude)
    decl_r = math.radians(decl)
    sin_elev = math.sin(lat_r) * math.sin(decl_r) + math.cos(lat_r) * math.cos(decl_r) * math.cos(
        math.radians(hour_angle)
    )
    sin_elev = max(-1.0, min(1.0, sin_elev))
    return math.degrees(math.asin(sin_elev))


def clear_sky_ghi(latitude: float, longitude: float, ts: dt.datetime) -> float:
    """Clear-sky global horizontal irradiance (W/m²) via the Haurwitz model.

    Returns 0 when the sun is below the horizon.
    """
    elevation = solar_elevation_deg(latitude, longitude, ts)
    if elevation <= 0.0:
        return 0.0
    cos_zenith = math.sin(math.radians(elevation))
    if cos_zenith <= 0.0:
        return 0.0
    ghi = _HAURWITZ_A * cos_zenith * math.exp(-_HAURWITZ_B / cos_zenith)
    return max(0.0, ghi)


def estimate_ghi(
    latitude: float, longitude: float, ts: dt.datetime, cloud_fraction: float
) -> float:
    """Estimate GHI (W/m²) at ``ts`` given cloud cover.

    ``cloud_fraction`` is the fraction of sky covered by cloud in ``[0, 1]``.
    Uses the Kasten–Czeplak attenuation: overcast skies still transmit ~25% of
    the clear-sky irradiance as diffuse light.
    """
    cf = max(0.0, min(1.0, cloud_fraction))
    clear = clear_sky_ghi(latitude, longitude, ts)
    if clear <= 0.0:
        return 0.0
    attenuation = 1.0 - 0.75 * (cf**3.4)
    return round(max(0.0, clear * attenuation), 1)
