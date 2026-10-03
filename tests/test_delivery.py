"""Verify Slack message bounds using mocked HTTP and image hosting."""

import unittest
from unittest.mock import Mock, patch

from delivery.slack_file import (
    SlackFileUploadError,
    _mrkdwn_sections,
    upload_png_report,
)
from processing.recommend import format_slack_recommendation


def large_recommendation() -> str:
    """Build a recommendation using the maximum supported monitor locations.

    Returns
    -------
    str
        Five forecast days across ten locations with long names.
    """
    reports = []
    for index in range(10):
        name = str(index) + " observing location" * 6
        reports.append(
            {
                "location": name,
                "timezone": "Asia/Yerevan",
                "days": [
                    {
                        "date": f"2026-10-{day:02d}",
                        "entries": [
                            {
                                "time": "20:00",
                                "models": {
                                    "ECMWF": {
                                        "cloud_low": 5,
                                        "cloud_mid": 10,
                                        "cloud_high": 20,
                                        "wind": 2,
                                        "temp": 12,
                                    }
                                },
                            }
                        ],
                    }
                    for day in range(3, 8)
                ],
            }
        )
    return format_slack_recommendation(reports)


class DeliveryTests(unittest.TestCase):
    """Protect recommendation content and existing image/link delivery behavior."""

    def setUp(self):
        """Install a fake successful HTTP response for every test message."""
        self.response = Mock()
        self.response.json.return_value = {"ok": True}
        self.post_patch = patch("delivery.slack_file.requests.post", return_value=self.response)
        self.post = self.post_patch.start()
        self.addCleanup(self.post_patch.stop)

    def test_supported_long_forecast_keeps_full_recommendation(self):
        """Accepted ten-location monitors remain within Slack section limits."""
        recommendation = large_recommendation()
        self.assertGreater(len(recommendation), 3000)
        upload_png_report(
            None, channel_id="C_TEST", bot_token="test-token", message_text=recommendation
        )
        payload = self.post.call_args.kwargs["json"]
        sections = payload["blocks"]
        self.assertEqual("".join(block["text"]["text"] for block in sections), recommendation)
        self.assertTrue(all(len(block["text"]["text"]) <= 3000 for block in sections))
        self.assertEqual(payload["text"], recommendation)
        self.assertEqual(self.post.call_args.args[0], "https://slack.com/api/chat.postMessage")

    def test_long_single_line_splits_without_losing_characters(self):
        """A line exceeding the section limit is split into bounded hard chunks."""
        text = "x" * 7000
        sections = _mrkdwn_sections(text, 50)
        self.assertEqual([len(block["text"]["text"]) for block in sections], [3000, 3000, 1000])
        self.assertEqual("".join(block["text"]["text"] for block in sections), text)

    def test_normal_line_boundaries_are_retained(self):
        """Ordinary forecast lines move intact to the following section."""
        text = "a" * 2000 + "\n" + "b" * 2000 + "\n" + "c" * 100
        sections = _mrkdwn_sections(text, 50)
        self.assertEqual(sections[0]["text"]["text"], "a" * 2000 + "\n")
        self.assertEqual("".join(block["text"]["text"] for block in sections), text)

    def test_block_cap_preserves_image_and_windy_links(self):
        """Oversized Sheet output reserves image/link blocks and marks truncation."""
        with patch(
            "delivery.slack_file._upload_to_temp_host",
            return_value="https://example.test/report.png",
        ) as host:
            upload_png_report(
                b"png",
                channel_id="C_TEST",
                bot_token="test-token",
                title="Report",
                message_text="x" * 200_000,
                windy_links=[("Test night", "https://www.windy.com/example")],
            )
        payload = self.post.call_args.kwargs["json"]
        blocks = payload["blocks"]
        self.assertEqual(len(blocks), 50)
        self.assertTrue(
            all(
                len(block["text"]["text"]) <= 3000 for block in blocks if block["type"] == "section"
            )
        )
        self.assertEqual(len(payload["text"]), 40_000)
        self.assertIn("truncated", payload["text"])
        self.assertIn("truncated", blocks[-3]["text"]["text"])
        self.assertEqual(
            blocks[-2],
            {"type": "image", "image_url": "https://example.test/report.png", "alt_text": "Report"},
        )
        self.assertIn("<https://www.windy.com/example|Test night>", blocks[-1]["text"]["text"])
        self.assertEqual(host.call_args.args[0], b"png")

    def test_short_recommendation_image_and_links_preserve_existing_shape(self):
        """Existing short reports keep their recommendation/image/Windy block order."""
        with patch(
            "delivery.slack_file._upload_to_temp_host",
            return_value="https://example.test/report.png",
        ):
            upload_png_report(
                b"png",
                channel_id="C_TEST",
                bot_token="test-token",
                title="Report",
                message_text="Clear skies",
                windy_links=[("Night", "https://www.windy.com/example")],
            )
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(
            [block["type"] for block in payload["blocks"]], ["section", "image", "section"]
        )
        self.assertEqual(payload["blocks"][0]["text"]["text"], "Clear skies")
        self.assertEqual(payload["text"], "Clear skies")

    def test_failed_image_host_still_delivers_text(self):
        """Failure of public image hosting retains the existing text-only fallback."""
        with patch("delivery.slack_file._upload_to_temp_host", return_value=None):
            upload_png_report(
                b"png", channel_id="C_TEST", bot_token="test-token", message_text="Cloudy"
            )
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(len(payload["blocks"]), 1)
        self.assertEqual(payload["text"], "Cloudy")

    def test_slack_rejection_remains_a_delivery_error(self):
        """Slack message rejection stays visible to the scheduler."""
        self.response.json.return_value = {"ok": False, "error": "invalid_blocks"}
        with self.assertRaisesRegex(SlackFileUploadError, "invalid_blocks"):
            upload_png_report(
                None, channel_id="C_TEST", bot_token="test-token", message_text="Cloudy"
            )
