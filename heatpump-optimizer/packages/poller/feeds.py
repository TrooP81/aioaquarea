"""External data feeds: electricity prices (ENTSO-E / Tibber) and weather (Open-Meteo)."""

from __future__ import annotations

import datetime as dt
from xml.etree import ElementTree

import httpx

from packages.core.config import settings
from packages.core.settings_service import get_setting

# ENTSO-E Transparency Platform day-ahead prices
ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"

# Tibber GraphQL API
TIBBER_URL = "https://api.tibber.com/v1-beta/gql"

# Open-Meteo free weather API
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


async def fetch_prices() -> list[tuple[dt.datetime, float]]:
    """
    Fetch electricity prices from the configured provider.
    Returns list of (timestamp_utc, EUR/kWh).
    """
    provider = await get_setting("price_provider")

    if provider == "manual":
        return await _get_manual_prices()
    elif provider == "tibber":
        return await _fetch_prices_tibber()
    return await _fetch_prices_entsoe()


async def _get_manual_prices() -> list[tuple[dt.datetime, float]]:
    """Generate static price entries for the next 48h using manual price setting."""
    price_str = await get_setting("manual_price_eur_per_kwh")
    try:
        price = float(price_str)
    except (ValueError, TypeError):
        price = 0.25

    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [(now + dt.timedelta(hours=h), price) for h in range(48)]


async def _fetch_prices_entsoe() -> list[tuple[dt.datetime, float]]:
    """
    Fetch day-ahead electricity prices from ENTSO-E for the next 24-48h.
    Returns list of (timestamp_utc, EUR/kWh).
    """
    token = await get_setting("entsoe_api_token")
    area = await get_setting("entsoe_area")

    if not token or token == "your_entsoe_token":
        return []

    now = dt.datetime.now(dt.timezone.utc)
    # Request from start of today to end of tomorrow
    period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period_end = period_start + dt.timedelta(days=2)

    params = {
        "securityToken": token,
        "documentType": "A44",  # Day-ahead prices
        "in_Domain": area,
        "out_Domain": area,
        "periodStart": period_start.strftime("%Y%m%d%H00"),
        "periodEnd": period_end.strftime("%Y%m%d%H00"),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(ENTSOE_URL, params=params)
        resp.raise_for_status()

    prices = _parse_entsoe_xml(resp.text, period_start)
    return prices


def _parse_entsoe_xml(xml_text: str, period_start: dt.datetime) -> list[tuple[dt.datetime, float]]:
    """Parse ENTSO-E day-ahead price XML into (timestamp, EUR/kWh) tuples."""
    results = []
    ns = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return results

    for time_series in root.findall(".//ns:TimeSeries", ns):
        period = time_series.find(".//ns:Period", ns)
        if period is None:
            continue

        start_elem = period.find("ns:timeInterval/ns:start", ns)
        if start_elem is None:
            continue

        start = dt.datetime.fromisoformat(start_elem.text.replace("Z", "+00:00"))

        for point in period.findall("ns:Point", ns):
            position = int(point.find("ns:position", ns).text)
            # Price is in EUR/MWh, convert to EUR/kWh
            price_mwh = float(point.find("ns:price.amount", ns).text)
            price_kwh = price_mwh / 1000.0

            ts = start + dt.timedelta(hours=position - 1)
            results.append((ts, price_kwh))

    return results


async def _fetch_prices_tibber() -> list[tuple[dt.datetime, float]]:
    """
    Fetch electricity prices from Tibber GraphQL API.
    Returns today's and tomorrow's prices as (timestamp_utc, EUR/kWh).
    """
    token = await get_setting("tibber_api_token")
    if not token:
        return []

    query = """
    {
      viewer {
        homes {
          currentSubscription {
            priceInfo {
              today {
                total
                startsAt
              }
              tomorrow {
                total
                startsAt
              }
            }
          }
        }
      }
    }
    """

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TIBBER_URL, json={"query": query}, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    results = []
    try:
        homes = data["data"]["viewer"]["homes"]
        if not homes:
            return results
        price_info = homes[0]["currentSubscription"]["priceInfo"]

        for price_entry in (price_info.get("today") or []) + (price_info.get("tomorrow") or []):
            ts = dt.datetime.fromisoformat(price_entry["startsAt"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            else:
                ts = ts.astimezone(dt.timezone.utc)
            # Tibber returns total price in the home's currency per kWh (inc. tax/fees)
            price_kwh = float(price_entry["total"])
            results.append((ts, price_kwh))
    except (KeyError, IndexError, TypeError):
        return results

    return results


async def fetch_weather() -> list[dict]:
    """
    Fetch 48h weather forecast from Open-Meteo (free, no API key) or manual static values.
    Returns list of dicts with ts, temperature, irradiance, wind_speed, humidity.
    """
    provider = await get_setting("weather_provider")
    if provider == "manual":
        return await _get_manual_weather()

    lat = await get_setting("latitude")
    lng = await get_setting("longitude")

    params = {
        "latitude": float(lat) if lat else settings.latitude,
        "longitude": float(lng) if lng else settings.longitude,
        "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,direct_radiation",
        "forecast_days": 2,
        "timezone": "UTC",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    humidity = hourly.get("relative_humidity_2m", [])
    wind = hourly.get("wind_speed_10m", [])
    radiation = hourly.get("direct_radiation", [])

    results = []
    for i, time_str in enumerate(times):
        ts = dt.datetime.fromisoformat(time_str).replace(tzinfo=dt.timezone.utc)
        results.append(
            {
                "ts": ts,
                "temperature": temps[i] if i < len(temps) else None,
                "humidity": humidity[i] if i < len(humidity) else None,
                "wind_speed": wind[i] if i < len(wind) else None,
                "irradiance": radiation[i] if i < len(radiation) else None,
            }
        )

    return results


async def _get_manual_weather() -> list[dict]:
    """Generate static weather entries for the next 48h using manual settings."""
    temp_str = await get_setting("manual_outdoor_temp")
    wind_str = await get_setting("manual_wind_speed")
    humidity_str = await get_setting("manual_humidity")
    irradiance_str = await get_setting("manual_irradiance")

    try:
        temp = float(temp_str)
    except (ValueError, TypeError):
        temp = 10.0
    try:
        wind = float(wind_str)
    except (ValueError, TypeError):
        wind = 5.0
    try:
        humidity = float(humidity_str)
    except (ValueError, TypeError):
        humidity = 60.0
    try:
        irradiance = float(irradiance_str)
    except (ValueError, TypeError):
        irradiance = 200.0

    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [
        {
            "ts": now + dt.timedelta(hours=h),
            "temperature": temp,
            "humidity": humidity,
            "wind_speed": wind,
            "irradiance": irradiance,
        }
        for h in range(48)
    ]
