"""External data feeds: electricity prices (ENTSO-E / Tibber) and weather (Open-Meteo / SMHI)."""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass

import defusedxml.ElementTree as ElementTree
import httpx

from packages.core.config import settings
from packages.core.settings_service import get_setting, get_setting_spec, get_string_setting
from packages.core.solar import clear_sky_ghi, estimate_ghi

# ENTSO-E Transparency Platform day-ahead prices
ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"

# Tibber GraphQL API
TIBBER_URL = "https://api.tibber.com/v1-beta/gql"

# Open-Meteo free weather API
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# SMHI Open Data point forecast (Nordic/Baltic coverage, no API key).
# Note: longitude precedes latitude in the URL path.
SMHI_URL = (
    "https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1"
    "/geotype/point/lon/{lon}/lat/{lat}/data.json"
)


@dataclass(frozen=True, slots=True)
class PriceFeed:
    """Hourly prices together with the currency supplied by the provider."""

    prices: list[tuple[dt.datetime, float]]
    currency: str
    source: str


class ProviderPriceList(list[tuple[dt.datetime, float]]):
    """List-compatible provider output carrying non-numeric price context."""

    def __init__(self, values: list[tuple[dt.datetime, float]], *, currency: str, source: str):
        super().__init__(values)
        self.currency = currency
        self.source = source


async def fetch_price_feed() -> PriceFeed:
    """
    Fetch electricity prices from the configured provider.
    Amounts stay in the provider's original currency. This prevents a Tibber
    SEK amount from being silently presented as EUR (or the reverse).
    """
    provider = await get_setting("price_provider")

    if provider == "manual":
        currency = (await get_string_setting("manual_price_currency") or "EUR").upper()
        return PriceFeed(await _get_manual_prices(), currency, "manual")
    elif provider == "tibber":
        prices = await _fetch_prices_tibber()
        return PriceFeed(list(prices), prices.currency, prices.source)
    prices = await _fetch_prices_entsoe()
    return PriceFeed(list(prices), getattr(prices, "currency", "EUR"), "entsoe")


async def fetch_prices() -> list[tuple[dt.datetime, float]]:
    """Backward-compatible price-only access for callers that do not persist data."""
    return (await fetch_price_feed()).prices


async def _get_manual_prices() -> list[tuple[dt.datetime, float]]:
    """Generate static price entries for the next 48h using manual price setting."""
    try:
        price_str = await get_setting("manual_price_eur_per_kwh")
    except ValueError:
        return []
    try:
        price = float(price_str)
    except (ValueError, TypeError):
        price = float(get_setting_spec("manual_price_eur_per_kwh").serialize_default())
    if not math.isfinite(price):
        return []

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


def _parse_entsoe_xml(xml_text: str, period_start: dt.datetime) -> ProviderPriceList:
    """Normalize completely covered hours, preserving currency and negative prices."""
    ns = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return ProviderPriceList([], currency="EUR", source="entsoe")
    currencies: set[str] = set()
    segments: dict[dt.datetime, set[tuple[dt.datetime, dt.datetime, float]]] = {}
    invalid_hours: set[dt.datetime] = set()
    for time_series in root.findall(".//ns:TimeSeries", ns):
        currency = time_series.findtext("ns:currency_Unit.name", namespaces=ns)
        unit = time_series.findtext("ns:price_Measure_Unit.name", namespaces=ns)
        if not currency or not re.fullmatch(r"[A-Z]{3}", currency) or unit != "MWH":
            return ProviderPriceList([], currency=currency or "EUR", source="entsoe")
        currencies.add(currency)
        for period in time_series.findall("ns:Period", ns):
            try:
                start = dt.datetime.fromisoformat(
                    period.findtext("ns:timeInterval/ns:start", namespaces=ns).replace(
                        "Z", "+00:00"
                    )
                )
                end = dt.datetime.fromisoformat(
                    period.findtext("ns:timeInterval/ns:end", namespaces=ns).replace("Z", "+00:00")
                )
                resolution = re.fullmatch(
                    r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
                    period.findtext("ns:resolution", namespaces=ns) or "",
                )
                if resolution is None or start.tzinfo is None or end.tzinfo is None:
                    return ProviderPriceList([], currency=currency, source="entsoe")
                duration = dt.timedelta(
                    hours=int(resolution[1] or 0),
                    minutes=int(resolution[2] or 0),
                    seconds=int(resolution[3] or 0),
                )
                if duration <= dt.timedelta(0) or end <= start:
                    return ProviderPriceList([], currency=currency, source="entsoe")
                start = start.astimezone(dt.timezone.utc)
                end = end.astimezone(dt.timezone.utc)
                for point in period.findall("ns:Point", ns):
                    position = int(point.findtext("ns:position", namespaces=ns))
                    price = float(point.findtext("ns:price.amount", namespaces=ns)) / 1000.0
                    left = start + duration * (position - 1)
                    right = left + duration
                    if position < 1 or right > end:
                        return ProviderPriceList([], currency=currency, source="entsoe")
                    while left < right:
                        hour = left.replace(minute=0, second=0, microsecond=0)
                        boundary = min(right, hour + dt.timedelta(hours=1))
                        if math.isfinite(price):
                            segments.setdefault(hour, set()).add((left, boundary, price))
                        else:
                            invalid_hours.add(hour)
                        left = boundary
            except (AttributeError, TypeError, ValueError, OverflowError):
                return ProviderPriceList([], currency=currency, source="entsoe")
    if len(currencies) != 1:
        return ProviderPriceList([], currency="EUR", source="entsoe")
    results = []
    for hour, portions in sorted(segments.items()):
        cursor, weighted = hour, 0.0
        valid = hour not in invalid_hours
        for left, right, price in sorted(portions):
            if left != cursor:
                valid = False
                break
            weighted += price * (right - left).total_seconds() / 3600.0
            cursor = right
        if valid and cursor == hour + dt.timedelta(hours=1):
            results.append((hour, weighted))
    return ProviderPriceList(results, currency=next(iter(currencies)), source="entsoe")


async def _fetch_prices_tibber() -> ProviderPriceList:
    token = await get_setting("tibber_api_token")
    if not token:
        return ProviderPriceList([], currency="EUR", source="tibber")

    query = """
    {
      viewer {
        homes {
          currentSubscription {
            priceInfo {
              today {
                total
                currency
                startsAt
              }
              tomorrow {
                total
                currency
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

    if data.get("errors"):
        return ProviderPriceList([], currency="EUR", source="tibber")

    homes = ((data.get("data") or {}).get("viewer") or {}).get("homes") or []
    if not homes:
        return ProviderPriceList([], currency="EUR", source="tibber")

    price_entries: list[dict] = []
    currency = "EUR"
    for home in homes:
        current_subscription = home.get("currentSubscription") or {}
        price_info = current_subscription.get("priceInfo") or {}
        price_entries = (price_info.get("today") or []) + (price_info.get("tomorrow") or [])
        if price_entries:
            currency = str(price_entries[0].get("currency") or "EUR").upper()
            break

    results: list[tuple[dt.datetime, float]] = []
    for price_entry in price_entries:
        starts_at = price_entry.get("startsAt")
        total = price_entry.get("total")
        if starts_at is None or total is None:
            continue

        ts = dt.datetime.fromisoformat(starts_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        else:
            ts = ts.astimezone(dt.timezone.utc)

        results.append((ts, float(total)))

    results.sort(key=lambda x: x[0])
    return ProviderPriceList(results, currency=currency, source="tibber")


async def fetch_weather() -> list[dict]:
    """
    Fetch 48h weather forecast from the configured provider (Open-Meteo or SMHI,
    both free and keyless) or manual static values.
    Returns list of dicts with ts, temperature, irradiance, wind_speed, humidity
    and precipitation (mm/h).
    """
    provider = await get_setting("weather_provider") or "open-meteo"
    if provider == "manual":
        result = await _get_manual_weather()
    elif provider == "smhi":
        result = await _fetch_weather_smhi()
    else:
        provider = "open-meteo"
        lat = await get_setting("latitude")
        lng = await get_setting("longitude")

        params = {
            "latitude": float(lat) if lat else settings.latitude,
            "longitude": float(lng) if lng else settings.longitude,
            "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,direct_radiation,cloud_cover,precipitation",
            "forecast_days": 2,
            "timezone": "UTC",
            "temperature_unit": "celsius",
            "wind_speed_unit": "ms",
            "precipitation_unit": "mm",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        result = _parse_open_meteo_forecast(data)

    issued_at = dt.datetime.now(dt.timezone.utc)
    for entry in result:
        entry["source"] = provider
        entry["forecast_issued_at"] = issued_at
        entry["units_schema"] = "weather_c_mps_wm2_mm_v1"
        for key in ("temperature", "humidity", "wind_speed", "irradiance", "precipitation"):
            entry[key] = _finite_provider_number(entry.get(key))
    return result


def _finite_provider_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _parse_open_meteo_forecast(data: dict) -> list[dict]:
    units = data.get("hourly_units")
    hourly = data.get("hourly")
    if not isinstance(units, dict) or not isinstance(hourly, dict):
        return []
    if (
        units.get("temperature_2m") != "°C"
        or units.get("wind_speed_10m") not in {"m/s", "km/h"}
        or units.get("direct_radiation") != "W/m²"
        or units.get("precipitation") != "mm"
    ):
        return []
    wind_scale = 1.0 / 3.6 if units["wind_speed_10m"] == "km/h" else 1.0
    fields = {
        "temperature": "temperature_2m",
        "humidity": "relative_humidity_2m",
        "wind_speed": "wind_speed_10m",
        "irradiance": "direct_radiation",
        "cloud_cover": "cloud_cover",
        "precipitation": "precipitation",
    }
    results = []
    for index, time_str in enumerate(hourly.get("time", [])):
        try:
            timestamp = dt.datetime.fromisoformat(time_str)
        except (TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
        row = {"ts": timestamp.astimezone(dt.timezone.utc)}
        for name, field in fields.items():
            values = hourly.get(field)
            value = _finite_provider_number(
                values[index] if isinstance(values, list) and index < len(values) else None
            )
            row[name] = value
        if row["wind_speed"] is not None:
            row["wind_speed"] *= wind_scale
        if row["cloud_cover"] is not None:
            row["cloud_cover"] /= 100.0
        results.append(row)
    return results


def _parse_smhi_forecast(
    data: dict, latitude: float, longitude: float, hours: int = 48
) -> list[dict]:
    """
    Parse an SMHI point-forecast JSON payload into hourly weather dicts.

    SMHI returns a variable-resolution forecast (hourly at the start, then 3, 6
    and 12h steps further out). This resamples it onto a contiguous hourly grid
    of ``hours`` entries via step (forward) fill, so it matches the hourly
    contract the optimizer and thermal model expect from Open-Meteo.

    SMHI has no solar-radiation parameter, but it reports total cloud cover in
    octas (``cloud_area_fraction``, 0–8). We reconstruct ``irradiance`` from the
    sun's position (via ``latitude``/``longitude``/time) attenuated by that
    cloud cover, so temperature predictions still reflect sunny vs overcast
    hours. Returns dicts with ts, temperature, humidity, wind_speed, cloud_cover
    (fraction 0–1), irradiance (W/m²), and precipitation (mm/h).
    """
    series: list[tuple[dt.datetime, dict]] = []
    for entry in data.get("timeSeries", []):
        time_str = entry.get("time")
        if not time_str:
            continue
        ts = dt.datetime.fromisoformat(time_str.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        params = entry.get("data") or {}
        series.append((ts, params))

    if not series:
        return []

    series.sort(key=lambda item: item[0])

    def _sample_at(target: dt.datetime) -> dict:
        """Return the most recent SMHI sample at or before ``target``."""
        chosen = series[0][1]
        for sample_ts, sample_params in series:
            if sample_ts <= target:
                chosen = sample_params
            else:
                break
        return chosen

    start = series[0][0].replace(minute=0, second=0, microsecond=0)
    results: list[dict] = []
    for h in range(hours):
        slot = start + dt.timedelta(hours=h)
        params = _sample_at(slot)
        temp = _finite_provider_number(params.get("air_temperature"))
        wind = _finite_provider_number(params.get("wind_speed"))
        humidity = _finite_provider_number(params.get("relative_humidity"))
        precipitation = _finite_provider_number(params.get("precipitation"))
        # Total cloud cover in octas (0–8) → fraction (0–1).
        octas = _finite_provider_number(params.get("cloud_area_fraction"))
        cloud_fraction = max(0.0, min(1.0, float(octas) / 8.0)) if octas is not None else None
        # Reconstruct irradiance from solar geometry, attenuated by cloud cover
        # (assume clear sky when cloud data is missing).
        if cloud_fraction is not None:
            irradiance = estimate_ghi(latitude, longitude, slot, cloud_fraction)
        else:
            irradiance = clear_sky_ghi(latitude, longitude, slot)
        results.append(
            {
                "ts": slot,
                "temperature": float(temp) if temp is not None else None,
                "humidity": float(humidity) if humidity is not None else None,
                "wind_speed": float(wind) if wind is not None else None,
                "cloud_cover": cloud_fraction,
                "irradiance": irradiance,
                "precipitation": float(precipitation) if precipitation is not None else None,
            }
        )

    return results


async def _fetch_weather_smhi() -> list[dict]:
    """
    Fetch a 48h weather forecast from SMHI Open Data (Nordic/Baltic, no API key).

    Returns list of dicts with ts, temperature, humidity, wind_speed,
    cloud_cover, precipitation and (solar-geometry-derived) irradiance.
    """
    lat = await get_setting("latitude")
    lng = await get_setting("longitude")
    latitude = float(lat) if lat else settings.latitude
    longitude = float(lng) if lng else settings.longitude

    # SMHI wants coordinates to max 6 decimals, longitude before latitude.
    url = SMHI_URL.format(lon=round(longitude, 6), lat=round(latitude, 6))

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    return _parse_smhi_forecast(data, latitude, longitude)


async def _get_manual_weather() -> list[dict]:
    """Generate static weather entries for the next 48h using manual settings."""
    temp_str = await get_setting("manual_outdoor_temp")
    wind_str = await get_setting("manual_wind_speed")
    humidity_str = await get_setting("manual_humidity")
    irradiance_str = await get_setting("manual_irradiance")
    precipitation_str = await get_setting("manual_precipitation")

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
    try:
        precipitation = max(0.0, float(precipitation_str))
    except (ValueError, TypeError):
        precipitation = 0.0

    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [
        {
            "ts": now + dt.timedelta(hours=h),
            "temperature": temp,
            "humidity": humidity,
            "wind_speed": wind,
            "irradiance": irradiance,
            "cloud_cover": None,
            "precipitation": precipitation,
        }
        for h in range(48)
    ]
