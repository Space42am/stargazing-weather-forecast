"""
Filter & reshape Open-Meteo responses into the report-ready structure.

Pipeline for one location:
    raw Open-Meteo JSON
      -> per-hour records with all model values attached
      -> filter by sun altitude (<= threshold)
      -> group by date

The output structure is fixed by the project spec; see ``build_location_report``.
"""

import logging
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

from config import (
    SUN_ALTITUDE_THRESHOLD_DEG,
    WEATHER_MODELS,
    WEATHER_VARIABLES,
)
from processing.sun import get_sun_altitude

logger = logging.getLogger(__name__)


# Maps Open-Meteo variable name -> short key used in the output schema.
# Centralised so the Slack formatter can rely on a stable contract.
_VARIABLE_OUTPUT_KEY: Dict[str, str] = {
    "temperature_2m":      "temp",
    "cloudcover_low":      "cloud_low",
    "cloudcover_mid":      "cloud_mid",
    "cloudcover_high":     "cloud_high",
    "windspeed_10m":       "wind",
    "windgusts_10m":       "gust",
    "relativehumidity_2m": "humidity",
    "pressure_msl":        "pressure",
}


def _resolve_timezone(payload: Dict[str, Any]) -> ZoneInfo:
    """
    Get the IANA timezone Open-Meteo used for this response.

    We asked for `timezone=auto`, so the server picks the local zone and
    echoes it back as `timezone`. Fall back to UTC defensively.
    """
    tz_name = payload.get("timezone") or "UTC"
    try:
        return ZoneInfo(tz_name)
    except Exception:  # pragma: no cover — extremely unlikely
        logger.warning("Unknown timezone %r from API; falling back to UTC", tz_name)
        return ZoneInfo("UTC")


def _series_for(
    hourly: Dict[str, Any],
    variable: str,
    model_id: str,
) -> Optional[List[Any]]:
    """
    Look up a variable's values for a specific model.

    Open-Meteo names per-model series like ``temperature_2m_gfs_seamless``.
    When only one model is requested it may also expose the unsuffixed
    name; we check both. Returns None if neither is present (model didn't
    cover this variable / location).
    """
    suffixed = f"{variable}_{model_id}"
    if suffixed in hourly:
        return hourly[suffixed]
    if variable in hourly:
        return hourly[variable]
    return None


def _build_models_block(
    hourly: Dict[str, Any],
    hour_index: int,
) -> Dict[str, Dict[str, Any]]:
    """
    Build the ``models`` sub-dict for a single hour.

    Skips any model that has no usable data (all values None or series
    missing entirely). This way a partial outage from one provider doesn't
    blank out the whole report.
    """
    models_block: Dict[str, Dict[str, Any]] = {}

    for model_label, model_id in WEATHER_MODELS.items():
        per_model: Dict[str, Any] = {}
        any_value = False

        for variable in WEATHER_VARIABLES:
            series = _series_for(hourly, variable, model_id)
            value = series[hour_index] if series and hour_index < len(series) else None
            if value is not None:
                any_value = True
            per_model[_VARIABLE_OUTPUT_KEY[variable]] = value

        if any_value:
            models_block[model_label] = per_model

    return models_block


def build_location_report(
    location_name: str,
    lat: float,
    lon: float,
    payload: Dict[str, Any],
    threshold_deg: float = SUN_ALTITUDE_THRESHOLD_DEG,
) -> Dict[str, Any]:
    """
    Convert one Open-Meteo response into the report-ready structure.

    Only includes the first 4 hours after sunset (when sun altitude ≤ threshold_deg).
    Days that end up empty after filtering are also dropped.
    """
    hourly = payload.get("hourly") or {}
    times: List[str] = hourly.get("time") or []
    tz = _resolve_timezone(payload)

    # OrderedDict preserves chronological day ordering as we encounter them.
    days: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()

    for i, raw_time in enumerate(times):
        # Open-Meteo returns ISO-8601 without timezone (because we asked
        # for local time via timezone=auto). Parse, then attach the zone.
        try:
            naive_dt = date_parser.isoparse(raw_time)
        except (ValueError, TypeError):
            logger.warning("Skipping unparseable timestamp %r", raw_time)
            continue
        local_dt = naive_dt.replace(tzinfo=tz) if naive_dt.tzinfo is None else naive_dt

        altitude = get_sun_altitude(lat, lon, local_dt)

        date_key = local_dt.strftime("%Y-%m-%d")

        if date_key not in days:
            days[date_key] = []

        # Only consider evening hours (18:00-23:59) for stargazing after sunset
        hour = local_dt.hour
        is_evening_hour = 18 <= hour <= 23

        if not days[date_key] and altitude <= threshold_deg and is_evening_hour:
            # First qualifying evening hour — include it and the next 3 from the same day
            sunset_date = local_dt.date()
            for j in range(4):
                if i + j >= len(times):
                    break
                try:
                    future_naive_dt = date_parser.isoparse(times[i + j])
                    future_dt = future_naive_dt.replace(tzinfo=tz) if future_naive_dt.tzinfo is None else future_naive_dt
                except (ValueError, TypeError):
                    continue

                if future_dt.date() != sunset_date:
                    break

                future_altitude = get_sun_altitude(lat, lon, future_dt)
                models_block = _build_models_block(hourly, i + j)
                if models_block:
                    days[date_key].append({
                        "time":         future_dt.strftime("%H:%M"),
                        "sun_altitude": round(future_altitude, 2),
                        "models":       models_block,
                    })
            continue

        if days[date_key]:
            continue

    return {
        "location": location_name,
        "days": [
            {"date": date_key, "entries": entries}
            for date_key, entries in days.items()
            if entries  # Only include days with entries
        ],
    }