"""Mocked adapter tests that never send notifications or fetch live weather."""

import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from threading import Event
from unittest.mock import patch

import locations
import main
import pipeline
from fetch.weather_api import WeatherFetchError

LOCATION = {"name": "Test location", "lat": 40.2, "lon": 44.5}
REPORT = {
    "location": "Test location",
    "timezone": "Asia/Yerevan",
    "days": [
        {
            "date": day,
            "entries": [
                {
                    "time": "20:00",
                    "models": {
                        "ECMWF": {
                            "cloud_low": 10,
                            "cloud_mid": 20,
                            "cloud_high": 30,
                            "temp": 12,
                            "wind": 2,
                        }
                    },
                }
            ],
        }
        for day in ("2026-10-03", "2026-10-04", "2026-10-05")
    ],
}


class PipelineTests(unittest.TestCase):
    """Exercise input isolation, cancellation, and delivery fallback."""

    def test_module_imports_do_not_fetch_sheet(self):
        """Configuration and pipeline imports perform no HTTP requests."""
        source = (
            "from unittest.mock import patch\n"
            "with patch('requests.get', side_effect=AssertionError('HTTP during import')):\n"
            "    import config, pipeline, main\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", source],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @patch("pipeline.build_location_report", side_effect=lambda *args: deepcopy(REPORT))
    @patch("pipeline.fetch_location_forecast", return_value={"timezone": "Asia/Yerevan"})
    def test_explicit_forecast_filters_event_dates_without_sheet(self, fetch, build):
        """Explicit coordinates use weather logic then inclusive date filtering."""
        with patch("pipeline.sheet_locations.get_locations") as sheet:
            result = pipeline.execute_report(
                [LOCATION],
                deliver=False,
                start_date="2026-10-04",
                end_date="2026-10-04",
            )
        sheet.assert_not_called()
        fetch.assert_called_once_with(40.2, 44.5)
        self.assertEqual([night["date"] for night in result["ranked_nights"]], ["2026-10-04"])
        self.assertIn("Test location", result["recommendation"])

    def test_invalid_event_window_fails_before_fetch(self):
        """An inverted event window cannot start network work."""
        with patch("pipeline.fetch_location_forecast") as fetch:
            with self.assertRaisesRegex(ValueError, "start_date"):
                pipeline.execute_report(
                    [LOCATION],
                    start_date="2026-10-05",
                    end_date="2026-10-03",
                )
        fetch.assert_not_called()

    @patch("pipeline.build_location_report", side_effect=lambda *args: deepcopy(REPORT))
    @patch("pipeline.fetch_location_forecast", return_value={"timezone": "Asia/Yerevan"})
    def test_event_outside_forecast_horizon_does_not_notify(self, fetch, build):
        """Future event monitors wait quietly until their dates enter the forecast."""
        with (
            patch("pipeline.screenshot_html") as screenshot,
            patch("pipeline.upload_png_report") as upload,
        ):
            result = pipeline.execute_report(
                [LOCATION], start_date="2026-11-01", end_date="2026-11-02"
            )
        self.assertEqual(result["recommendation"], "")
        self.assertEqual(result["ranked_nights"], [])
        self.assertEqual(result["reports"][0]["days"], [])
        screenshot.assert_not_called()
        upload.assert_not_called()

    @patch(
        "pipeline.build_all_reports",
        return_value=([{"location": "Unavailable", "days": [], "_error": "offline"}], None),
    )
    def test_legacy_unfiltered_fetch_failure_retains_delivery(self, build):
        """Legacy Sheet failures still notify even when no forecast is available."""
        with patch("pipeline.screenshot_html", return_value=b"png") as screenshot:
            with patch("pipeline.upload_png_report") as upload:
                pipeline.execute_report([LOCATION])
        screenshot.assert_called_once()
        upload.assert_called_once()

    def test_already_stopped_report_does_no_work(self):
        """Cancellation before execution prevents all fetch and delivery work."""
        with (
            patch("pipeline.fetch_location_forecast") as fetch,
            patch("pipeline.upload_png_report") as upload,
        ):
            with self.assertRaises(pipeline.ReportCancelled):
                pipeline.execute_report([LOCATION], cancelled=lambda: True)
        fetch.assert_not_called()
        upload.assert_not_called()

    def test_stop_between_fetches_prevents_next_location(self):
        """Stopping during a fetch prevents subsequent work and notification."""
        stopped = Event()

        def fetch_and_stop(*args):
            """Return a mocked forecast after requesting cancellation."""
            stopped.set()
            return {"timezone": "Asia/Yerevan"}

        with patch("pipeline.fetch_location_forecast", side_effect=fetch_and_stop) as fetch:
            with patch("pipeline.upload_png_report") as upload:
                with self.assertRaises(pipeline.ReportCancelled):
                    pipeline.execute_report([LOCATION, LOCATION], cancelled=stopped.is_set)
        self.assertEqual(fetch.call_count, 1)
        upload.assert_not_called()

    @patch("pipeline.build_all_reports", return_value=([REPORT], None))
    def test_stop_during_screenshot_prevents_delivery(self, build):
        """A stop received while Chromium runs is checked before Slack delivery."""
        stopped = Event()

        def screenshot_and_stop(html):
            """Return a mocked image after cancellation.

            Parameters
            ----------
            html : str
                Unused report document.

            Returns
            -------
            bytes
                Mocked PNG bytes.
            """
            stopped.set()
            return b"png"

        with patch("pipeline.screenshot_html", side_effect=screenshot_and_stop):
            with patch("pipeline.upload_png_report") as upload:
                with self.assertRaises(pipeline.ReportCancelled):
                    pipeline.execute_report([LOCATION], cancelled=stopped.is_set)
        upload.assert_not_called()

    @patch("pipeline.build_all_reports", return_value=([REPORT], None))
    def test_screenshot_failure_keeps_text_delivery(self, build):
        """Image rendering failures retain the legacy recommendation fallback."""
        with patch("pipeline.screenshot_html", side_effect=RuntimeError("browser unavailable")):
            with patch("pipeline.upload_png_report") as upload:
                result = pipeline.execute_report([LOCATION], channel_id="C_TEST")
        self.assertIsNone(upload.call_args.args[0])
        self.assertEqual(upload.call_args.kwargs["channel_id"], "C_TEST")
        self.assertEqual(upload.call_args.kwargs["message_text"], result["recommendation"])

    @patch("pipeline.build_location_report", side_effect=lambda *args: deepcopy(REPORT))
    def test_partial_forecast_failure_preserves_successful_locations(self, build):
        """One unavailable location does not discard other forecast results."""
        with patch(
            "pipeline.fetch_location_forecast", side_effect=[WeatherFetchError("offline"), {}]
        ):
            reports, metadata = pipeline.build_all_reports([LOCATION, LOCATION])
        self.assertEqual(reports[0]["_error"], "offline")
        self.assertEqual(reports[1]["location"], "Test location")
        self.assertIsNotNone(metadata)

    @patch("pipeline.execute_report", return_value={"recommendation": "ok", "ranked_nights": []})
    @patch(
        "pipeline.sheet_locations.get_locations",
        side_effect=[[LOCATION], [dict(LOCATION, name="New location")]],
    )
    def test_sheet_adapter_refreshes_rows_each_run(self, load, execute):
        """Repeated reports read current Sheet rows instead of cached startup data."""
        pipeline.execute_sheet_report("C_TEST")
        pipeline.execute_sheet_report("C_TEST")
        self.assertEqual(load.call_count, 2)
        self.assertEqual(execute.call_args.args[0][0]["name"], "New location")

    @patch("pipeline.sheet_locations.get_locations", return_value=[])
    def test_empty_sheet_is_observable_failure(self, load):
        """Missing Sheet input produces a visible failed execution."""
        with self.assertRaisesRegex(RuntimeError, "No locations loaded"):
            pipeline.execute_sheet_report()

    @patch("pipeline.is_in_notification_window", return_value=False)
    @patch(
        "pipeline.sheet_locations.get_locations",
        return_value=[dict(LOCATION, preferred_period="May 22 2099")],
    )
    def test_inactive_sheet_does_not_notify(self, load, window):
        """A valid Sheet with no active rows succeeds without notification."""
        with patch("pipeline.execute_report") as execute:
            result = pipeline.execute_sheet_report()
        self.assertEqual(result, {"recommendation": "", "ranked_nights": [], "reports": []})
        execute.assert_not_called()

    def test_dropped_sheet_metadata_keeps_existing_references(self):
        """Reloading deduplication metadata does not leave imported stale lists."""
        reference = locations.DROPPED_LOCATIONS
        second = dict(LOCATION, name="Nearby", lat=40.201)
        try:
            kept = locations._deduplicate_nearby([LOCATION, second])
            self.assertEqual(kept, [LOCATION])
            self.assertIs(locations.DROPPED_LOCATIONS, reference)
            self.assertEqual(reference[0]["name"], "Nearby")
            locations._deduplicate_nearby([LOCATION])
            self.assertEqual(reference, [])
        finally:
            reference.clear()

    @patch("pipeline.build_all_reports", return_value=([REPORT], None))
    def test_saved_report_does_not_delete_other_runs(self, build):
        """Legacy output writes only its supplied destination."""
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "weather_report_other.html"
            other.write_text("other run")
            daily = Path(directory) / "weather_report_daily.html"
            pipeline.execute_report([LOCATION], deliver=False, output_path=str(daily))
            self.assertEqual(other.read_text(), "other run")
            self.assertIn("Test location", daily.read_text())

    @patch("main.execute_sheet_report", side_effect=RuntimeError("Sheet unavailable"))
    def test_legacy_main_failure_returns_nonzero(self, execute):
        """Daily job failure remains visible to its external scheduler."""
        self.assertEqual(main.main(), 1)
