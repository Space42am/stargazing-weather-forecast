"""
Central configuration for the weather report pipeline.

All tunables live here. Nothing in this file should require code changes
to ship to a new environment — environment variables override defaults
where it makes sense (e.g. the Slack webhook URL).
"""

import os
from typing import List, Dict, Any

from dotenv import load_dotenv

load_dotenv()  # loads .env from project root if present

from locations import get_locations


# --- Slack -------------------------------------------------------------------

SLACK_WEBHOOK_URL: str = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_BOT_TOKEN: str   = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID: str  = os.environ.get("SLACK_CHANNEL_ID", "")
SLACK_APP_TOKEN: str   = os.environ.get("SLACK_APP_TOKEN", "")
IMGBB_API_KEY: str     = os.environ.get("IMGBB_API_KEY", "")


# --- Locations ---------------------------------------------------------------

# Locations are dynamically loaded from Google Sheets
LOCATIONS: List[Dict[str, Any]] = get_locations()


# --- Forecast horizon --------------------------------------------------------

FORECAST_DAYS: int = 5


# --- Weather variables (Open-Meteo names) -----------------------------------

WEATHER_VARIABLES: List[str] = [
    "temperature_2m",
    "cloudcover_low",
    "cloudcover_mid",
    "cloudcover_high",
    "windspeed_10m",
    "windgusts_10m",
    "relativehumidity_2m",
    "pressure_msl",
]


# --- Weather models ---------------------------------------------------------

# Open-Meteo identifiers. The unified /v1/forecast endpoint accepts a
# comma-separated `models=` parameter and returns variables suffixed with
# the model name, e.g. `temperature_2m_gfs_seamless`.
#
# Maps human-friendly label -> Open-Meteo model id. The label is what
# appears in the Slack output; the id is what we send on the wire.
WEATHER_MODELS: Dict[str, str] = {
    "GFS":   "gfs_seamless",
    "ICON":  "icon_seamless",
    "ECMWF": "ecmwf_ifs025",
}


# --- Sun-altitude filter ----------------------------------------------------

# Hours where the sun is at or below this altitude (degrees) are kept.
# -4° corresponds roughly to the start of nautical twilight on the dark side
# of civil twilight — i.e. the sky is dim enough for astronomy.
SUN_ALTITUDE_THRESHOLD_DEG: float = 20.0


# --- HTTP --------------------------------------------------------------------

OPEN_METEO_URL: str = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT_SECONDS: int = 20
SLACK_TIMEOUT_SECONDS: int = 10
SLACK_RETRY_COUNT: int = 1  # one retry on top of the initial attempt
