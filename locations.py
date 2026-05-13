"""
Dynamic location loading from Google Sheets.

Fetches registered locations from a Google Sheet and geocodes them to coordinates.
"""

import csv
import math
import time
import requests
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse


# Google Sheets configuration
SPREADSHEET_ID = "1YtLyXxU3nPAdL5LDcTimX3uMh0AxC0IXsdNSTVB4Gig"
SHEET_GID = "2107805094"  # "Գրանցված, հետևում ենք եղանակին" subsheet
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/export?format=csv&gid={SHEET_GID}"

# Location columns
PREFERRED_PERIOD_COLUMN_INDEX = 8   # Column I (0-indexed) — preferred visit period
LOCATION_COLUMN_INDEX         = 11  # Column L (0-indexed)
PROVINCE_COLUMN_INDEX         = 12  # Column M (0-indexed)
LOCATION_START_ROW            = 2   # Start from row 2 (1-indexed, skip header)


class LocationError(Exception):
    """Error fetching or geocoding locations."""
    pass


def geocode_location(location_name: str, province_name: str = "") -> Optional[Dict[str, Any]]:
    """
    Geocode a location name and province to latitude/longitude using Nominatim.

    Returns dict with 'name', 'province', 'lat', 'lon' or None if geocoding fails.
    """
    if not location_name or not location_name.strip():
        return None

    location_name = location_name.strip()
    province_name = province_name.strip() if province_name else ""

    # Build search query with province for better accuracy
    if province_name:
        search_query = f"{location_name}, {province_name}, Armenia"
    else:
        search_query = f"{location_name}, Armenia"

    # Nominatim API endpoint
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": search_query,
        "format": "json",
        "limit": 1,
        "countrycodes": "AM",  # Limit to Armenia for better accuracy
    }
    headers = {
        "User-Agent": "WeatherForecastBot/1.0"
    }

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()

        data = response.json()
        if data:
            result = data[0]
            return {
                "name": location_name,
                "province": province_name,
                "display_name": f"{location_name}, {province_name}" if province_name else location_name,
                "lat": float(result["lat"]),
                "lon": float(result["lon"]),
            }

    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"Warning: Failed to geocode '{search_query}': {exc}")

    return None


def fetch_locations_from_sheet() -> List[Dict[str, Any]]:
    """
    Fetch locations from Google Sheets CSV export.

    Reads column L (index 11) starting from row 2, geocodes each location,
    and returns list of location dictionaries.

    Note: The Google Sheet must be publicly viewable for this to work.
    Go to File > Share > 'Anyone with the link can view'.
    """
    locations = []

    try:
        print(f"Fetching locations from Google Sheet...")
        print(f"URL: {CSV_URL}")
        
        # Try with various headers to bypass potential access restrictions
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        print(f"Headers: {headers}")
        response = requests.get(CSV_URL, timeout=10, headers=headers, allow_redirects=True)
        print(f"Response status: {response.status_code}")
        response.raise_for_status()

        # Decode with UTF-8 to handle Armenian characters properly
        csv_text = response.content.decode('utf-8')
        print(f"Decoded {len(csv_text)} characters")

        # Parse CSV
        csv_reader = csv.reader(csv_text.splitlines())
        rows = list(csv_reader)

        if len(rows) < LOCATION_START_ROW:
            raise LocationError(f"Sheet has only {len(rows)} rows, expected at least {LOCATION_START_ROW}")

        # Process rows starting from LOCATION_START_ROW (1-indexed)
        for row_idx in range(LOCATION_START_ROW - 1, len(rows)):
            row = rows[row_idx]

            # Check if row has enough columns
            if len(row) <= max(LOCATION_COLUMN_INDEX, PROVINCE_COLUMN_INDEX):
                continue

            location_name    = row[LOCATION_COLUMN_INDEX].strip()
            province_name    = row[PROVINCE_COLUMN_INDEX].strip() if len(row) > PROVINCE_COLUMN_INDEX else ""
            preferred_period = row[PREFERRED_PERIOD_COLUMN_INDEX].strip() if len(row) > PREFERRED_PERIOD_COLUMN_INDEX else ""

            if not location_name:
                continue

            print(f"Geocoding location: {location_name}" + (f", {province_name}" if province_name else ""))

            # Geocode the location with province for accuracy
            location_data = geocode_location(location_name, province_name)
            if location_data:
                location_data["preferred_period"] = preferred_period
                locations.append(location_data)
                print(f"✓ Geocoded: {location_name}, {province_name}")
            else:
                print(f"Warning: Could not geocode '{location_name}, {province_name}', skipping")

            # Rate limiting for Nominatim (1 request per second max)
            time.sleep(1.1)

        print(f"Successfully loaded {len(locations)} locations from sheet")

    except requests.HTTPError as exc:
        if exc.response.status_code == 404:
            raise LocationError(
                f"❌ Google Sheet not accessible (404 - NOT FOUND)\n\n"
                f"The sheet URL is incorrect or the sheet doesn't exist.\n"
                f"Please verify your sheet ID is correct:\n\n"
                f"1. Open your sheet in browser: https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit\n"
                f"2. Copy the Sheet ID from between '/d/' and '/edit'\n"
                f"3. Update SPREADSHEET_ID in locations.py with the correct ID\n"
                f"4. Make sure sharing is set to 'Anyone with the link can view'\n"
                f"5. Try again\n\n"
                f"Current Sheet ID in code: {SPREADSHEET_ID}"
            )
        else:
            raise LocationError(f"Failed to fetch Google Sheet (HTTP {exc.response.status_code}): {exc}")
    except requests.RequestException as exc:
        raise LocationError(f"Failed to fetch Google Sheet (connection error): {exc}")
    except Exception as exc:
        raise LocationError(f"Error processing locations: {exc}")

    return locations


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


DROPPED_LOCATIONS: List[Dict[str, Any]] = []


def _deduplicate_nearby(locations: List[Dict[str, Any]], radius_km: float = 20) -> List[Dict[str, Any]]:
    global DROPPED_LOCATIONS
    DROPPED_LOCATIONS = []
    kept: List[Dict[str, Any]] = []
    for loc in locations:
        close_to = next(
            (k for k in kept if _haversine_km(loc["lat"], loc["lon"], k["lat"], k["lon"]) < radius_km),
            None,
        )
        if close_to:
            dist = _haversine_km(loc["lat"], loc["lon"], close_to["lat"], close_to["lon"])
            print(f"Dropped '{loc['name']}' ({dist:.1f} km from '{close_to['name']}')")
            DROPPED_LOCATIONS.append({"name": loc["name"], "close_to": close_to["name"], "dist_km": dist})
        else:
            kept.append(loc)
    return kept


def get_locations() -> List[Dict[str, Any]]:
    """
    Get locations from Google Sheets.

    Returns geocoded locations from the sheet. If fetching fails,
    returns empty list (no hardcoded fallbacks - sheet must be accessible).
    """
    try:
        locations = fetch_locations_from_sheet()
        if locations:
            locations = _deduplicate_nearby(locations)
            print(f"Successfully loaded {len(locations)} locations from Google Sheet")
            return locations
        else:
            print("Warning: No locations found in Google Sheet")
            return []
    except LocationError as exc:
        print(f"Error fetching locations from Google Sheet: {exc}")
        return []