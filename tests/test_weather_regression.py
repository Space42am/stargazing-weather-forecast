"""Synthetic forecasts lock existing weather calculations and HTML safety."""

import json
import unittest
from datetime import date
from unittest.mock import patch

from config import SUN_ALTITUDE_THRESHOLD_DEG, WEATHER_MODELS
from formatting.html_formatter import _script_json, render_html
from processing.filter import build_location_report
from processing.recommend import rank_nights
from processing.schedule import is_in_notification_window


def synthetic_forecast():
    """Build six hourly readings for each existing weather model.

    Returns
    -------
    dict
        Open-Meteo-shaped hourly payload with deterministic values.
    """
    hourly = {"time": [f"2026-10-03T{hour}:00" for hour in range(18, 24)]}
    cloud = {"GFS": 20, "ICON": 10, "ECMWF": 5}
    temp = {"GFS": 10, "ICON": 20, "ECMWF": 30}
    wind = {"GFS": 2, "ICON": 3, "ECMWF": 4}
    for label, model_id in WEATHER_MODELS.items():
        for variable, values in {
            "temperature_2m": [temp[label] - offset for offset in range(6)],
            "cloudcover_low": [cloud[label]] * 6,
            "cloudcover_mid": [cloud[label] + 10] * 6,
            "cloudcover_high": [cloud[label] + 20] * 6,
            "windspeed_10m": [wind[label]] * 6,
        }.items():
            hourly[f"{variable}_{model_id}"] = values
    return {"timezone": "Asia/Yerevan", "hourly": hourly}


class WeatherRegressionTests(unittest.TestCase):
    """Protect existing selection, scoring, schedule, and chart values."""

    def test_existing_four_evening_hours_and_scores(self):
        """Preserve the +20 degree threshold and weighted scoring exactly."""
        payload = synthetic_forecast()
        with patch(
            "processing.filter.get_sun_altitude",
            side_effect=lambda lat, lon, dt: 21 if dt.hour == 18 else 19,
        ):
            report = build_location_report("Observed location", 40.2, 44.5, payload)
        self.assertEqual(SUN_ALTITUDE_THRESHOLD_DEG, 20.0)
        self.assertEqual(
            [entry["time"] for entry in report["days"][0]["entries"]],
            ["19:00", "20:00", "21:00", "22:00"],
        )
        self.assertEqual(
            rank_nights([report]),
            [
                {
                    "location": "Observed location",
                    "date": "2026-10-03",
                    "hour": "19",
                    "windy_date": "2026-10-03",
                    "windy_hour": "15",
                    "cloud": 14.5,
                    "wind": 3.0,
                    "min_temp": 16.0,
                    "label": "Good",
                }
            ],
        )

    def test_wind_threshold_and_cloud_order_preserved(self):
        """Exactly four m/s yields Windy and sinks below lower cloud ranks."""
        reports = []
        for name, cloud, wind in [("windy clear", 0, 4), ("cloudier", 19, 2), ("clear", 5, 2)]:
            reports.append(
                {
                    "location": name,
                    "timezone": "UTC",
                    "days": [
                        {
                            "date": "2026-10-03",
                            "entries": [
                                {
                                    "time": "20:00",
                                    "models": {
                                        "ECMWF": {
                                            "cloud_low": cloud,
                                            "cloud_mid": cloud,
                                            "cloud_high": cloud,
                                            "wind": wind,
                                        }
                                    },
                                }
                            ],
                        }
                    ],
                }
            )
        ranked = rank_nights(reports)
        self.assertEqual(
            [night["location"] for night in ranked], ["clear", "cloudier", "windy clear"]
        )
        self.assertEqual(ranked[-1]["label"], "Windy")

    def test_existing_sheet_date_window(self):
        """Single-date and recognized range scheduling retain their boundaries."""
        self.assertFalse(is_in_notification_window("May 22 2026", date(2026, 5, 14)))
        self.assertTrue(is_in_notification_window("May 22 2026", date(2026, 5, 15)))
        self.assertTrue(is_in_notification_window("May 22 2026", date(2027, 5, 15)))
        self.assertTrue(is_in_notification_window("May 1 2026 to May 22 2026", date(2026, 5, 22)))
        self.assertFalse(is_in_notification_window("May 1 2026 to May 22 2026", date(2026, 5, 23)))
        self.assertTrue(is_in_notification_window("unrecognized", date(2026, 5, 1)))

    def test_html_title_and_location_escape_preserve_chart_values(self):
        """Untrusted labels cannot introduce HTML or terminate inline scripts."""
        name = "</h2><script>alert('location')</script>"
        header = "</title><img src=x onerror=alert(1)>"
        report = {
            "location": name,
            "days": [
                {
                    "date": "2026-10-03",
                    "entries": [
                        {
                            "time": "20:00",
                            "models": {
                                "</script><script>alert(1)</script>": {
                                    "cloud_low": 17,
                                    "cloud_mid": 23,
                                    "cloud_high": 31,
                                    "temp": 9,
                                    "wind": 2,
                                }
                            },
                        }
                    ],
                }
            ],
        }
        html = render_html([report], header=header)
        self.assertNotIn(name, html)
        self.assertNotIn(header, html)
        self.assertNotIn("</script><script>alert(1)</script>", html)
        self.assertIn("&lt;/h2&gt;", html)
        self.assertIn('"data": [17]', html)

    def test_script_encoding_roundtrips_original_values(self):
        """Safe JSON escaping does not alter the chart's string or numeric data."""
        original = {"label": "</script>&\u2028\u2029", "values": [0, 12.5, None]}
        encoded = _script_json(original)
        self.assertNotIn("</script>", encoded)
        self.assertEqual(json.loads(encoded), original)
