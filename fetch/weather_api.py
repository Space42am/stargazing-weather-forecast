"""
Open-Meteo client.

Single responsibility: given a location, return raw hourly forecast data
for the configured variables and models. No filtering, no scoring, no
interpretation — that lives downstream.
"""

import logging
from typing import Dict, List, Optional, Any

import requests

from config import (
    OPEN_METEO_URL,
    FORECAST_DAYS,
    WEATHER_VARIABLES,
    WEATHER_MODELS,
    HTTP_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


class WeatherFetchError(Exception):
    """Raised when we cannot retrieve usable data for a location."""


def fetch_location_forecast(
    lat: float,
    lon: float,
    forecast_days: int = FORECAST_DAYS,
    variables: Optional[List[str]] = None,
    models: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Fetch hourly multi-model forecast for one location.

    Open-Meteo returns variables suffixed by model id, e.g.
    ``temperature_2m_gfs_seamless``. We don't reshape that here — the
    caller (filter/processing layer) handles the un-suffixing. Keeping
    this layer dumb makes it trivial to retry, mock, or cache.

    Args:
        lat, lon: coordinates
        forecast_days: how many days forward to request
        variables: hourly variable names (Open-Meteo naming)
        models: {label: open_meteo_model_id}

    Returns:
        Parsed JSON dict from Open-Meteo.

    Raises:
        WeatherFetchError: on timeout, network error, HTTP error, or an
            API-level error response.
    """
    variables = variables or WEATHER_VARIABLES
    models = models or WEATHER_MODELS

    params = {
        "latitude":        lat,
        "longitude":       lon,
        "hourly":          ",".join(variables),
        "models":          ",".join(models.values()),
        "forecast_days":   forecast_days,
        # `auto` makes Open-Meteo pick the location's timezone and emit
        # ISO timestamps in local time. That keeps "date" / "time" in the
        # final report aligned with the observer, not UTC.
        "timezone":        "auto",
        # Default unit for windspeed_10m is km/h — request m/s explicitly.
        "wind_speed_unit": "ms",
    }

    try:
        response = requests.get(
            OPEN_METEO_URL,
            params=params,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise WeatherFetchError(
            f"Timeout fetching forecast for ({lat}, {lon})"
        ) from exc
    except requests.RequestException as exc:
        raise WeatherFetchError(
            f"Network error fetching forecast for ({lat}, {lon}): {exc}"
        ) from exc

    if not response.ok:
        raise WeatherFetchError(
            f"Open-Meteo HTTP {response.status_code} for ({lat}, {lon}): "
            f"{response.text[:200]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise WeatherFetchError(
            f"Open-Meteo returned non-JSON for ({lat}, {lon})"
        ) from exc

    # Open-Meteo signals errors with `{"error": true, "reason": "..."}`
    # and HTTP 400 — but defend against the body shape regardless.
    if isinstance(payload, dict) and payload.get("error"):
        raise WeatherFetchError(
            f"Open-Meteo error for ({lat}, {lon}): {payload.get('reason')}"
        )

    if "hourly" not in payload or "time" not in payload.get("hourly", {}):
        raise WeatherFetchError(
            f"Open-Meteo response missing hourly block for ({lat}, {lon})"
        )

    return payload