"""
Sun-altitude utilities.

Thin wrapper around `astral` so the rest of the code never imports it
directly. Keeps the dependency swappable (e.g. for `pyephem` or `skyfield`)
and gives us a single place to reason about timezone handling.
"""

from datetime import datetime, timezone

from astral import Observer
from astral.sun import elevation


def get_sun_altitude(lat: float, lon: float, dt: datetime) -> float:
    """
    Return the sun's altitude above the horizon, in degrees.

    Negative values mean the sun is below the horizon. The result includes
    atmospheric refraction (astral's default).

    Args:
        lat: latitude in decimal degrees (north positive)
        lon: longitude in decimal degrees (east positive)
        dt:  the moment to evaluate. **Must be timezone-aware** — naive
             datetimes are treated as UTC by astral, which silently
             produces wrong altitudes for non-UTC observers. We refuse
             naive input so the bug can't sneak in.

    Returns:
        Altitude in degrees, e.g. -4.7 means just below the horizon.
    """
    if dt.tzinfo is None:
        raise ValueError(
            "get_sun_altitude requires a timezone-aware datetime; "
            "got naive value (would be misinterpreted as UTC)."
        )

    observer = Observer(latitude=lat, longitude=lon)
    return elevation(observer, dt.astimezone(timezone.utc))