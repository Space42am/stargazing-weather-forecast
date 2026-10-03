"""Verify weather API authentication, validation and durable lifecycle boundaries."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitoring import MonitorError, validate_request
from service import create_app

LOCATION = {"name": "Byurakan", "lat": 40.34, "lon": 44.27}


class ServiceTests(unittest.TestCase):
    """Exercise the real Flask API against an isolated durable database."""

    def setUp(self):
        """Create a test client without starting weather network work."""
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "monitors.sqlite")
        self.config = {
            "TESTING": True,
            "START_SCHEDULER": False,
            "WEATHER_API_TOKEN": "test-token",
            "WEATHER_DB_PATH": self.path,
            "WEATHER_SHEET_ENABLED": True,
            "SLACK_BOT_TOKEN": "test-slack-token",
            "SLACK_CHANNEL_ID": "C12345678",
        }
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer test-token", "Idempotency-Key": "request-one"}

    def tearDown(self):
        """Remove isolated database files."""
        self.directory.cleanup()

    def create(self, payload=None, key="request-one"):
        """Submit an authenticated create request with a stable idempotency key.

        Parameters
        ----------
        payload : dict, optional
            Monitor creation body.
        key : str, optional
            Idempotency key for this request.

        Returns
        -------
        TestResponse
            Flask client response.
        """
        return self.client.post(
            "/v1/monitors",
            json=payload or {"locations": [LOCATION]},
            headers={**self.headers, "Idempotency-Key": key},
        )

    def test_public_health_and_every_versioned_route_require_auth(self):
        """Keep health public while rejecting missing or incorrect API tokens."""
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        for path in ("/v1/monitors", "/v1/monitors/sheet", "/v1/unknown"):
            self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(
                self.client.get(path, headers={"Authorization": "Bearer wrong"}).status_code, 401
            )
        self.assertEqual(self.client.post("/v1/monitors/sheet/stop").status_code, 401)

    def test_idempotency_replays_across_app_restart_and_rejects_conflicts(self):
        """Preserve one durable monitor after retries and service restarts."""
        first = self.create()
        self.assertEqual(first.status_code, 201)
        self.client = create_app(self.config).test_client()
        replay = self.create()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.json["id"], replay.json["id"])
        conflict = self.create({"locations": [LOCATION], "interval_seconds": 3600})
        self.assertEqual(conflict.status_code, 409)
        self.assertNotIn("idempotency_key", first.json)

    def test_validation_rejects_origin_unknowns_and_unbounded_inputs(self):
        """Reject unsupported ownership fields and dangerous coordinate/schedule values."""
        cases = [
            {"locations": [LOCATION], "source": "sheet"},
            {"locations": [LOCATION], "origin": "sheet"},
            {"locations": []},
            {"locations": [LOCATION] * 11},
            {"locations": [{**LOCATION, "lat": True}]},
            {"locations": [{**LOCATION, "lat": 91}]},
            {"locations": [{**LOCATION, "lon": float("nan")}]},
            {"locations": [{**LOCATION, "lon": 10**1000}]},
            {"locations": [{**LOCATION, "name": " "}]},
            {"locations": [{**LOCATION, "extra": "ignored"}]},
            {"locations": [LOCATION], "interval_seconds": 10},
            {"locations": [LOCATION], "interval_seconds": True},
            {"locations": [LOCATION], "channel_id": "#general"},
            {"locations": [LOCATION], "start_date": "2026-02-30"},
            {"locations": [LOCATION], "start_date": "2026-10-10", "end_date": "2026-10-09"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(self.create(payload).status_code, 400)
        no_key = self.client.post(
            "/v1/monitors",
            json={"locations": [LOCATION]},
            headers={"Authorization": "Bearer test-token"},
        )
        self.assertEqual(no_key.status_code, 400)

    def test_sheet_cannot_be_stopped_and_marvin_stop_has_receipt(self):
        """Enforce source ownership even when the caller knows the stable Sheet ID."""
        response = self.client.post("/v1/monitors/sheet/stop", headers=self.headers)
        self.assertEqual(response.status_code, 403)
        monitor = self.create().json
        stop = self.client.post(f"/v1/monitors/{monitor['id']}/stop", headers=self.headers)
        self.assertEqual(stop.status_code, 200)
        self.assertTrue(stop.json["cancellation_confirmed"])
        self.assertEqual(stop.json["monitor"]["status"], "stopped")
        self.assertEqual(
            self.client.get("/v1/monitors/sheet", headers=self.headers).json["status"], "active"
        )
        self.assertEqual(
            len(
                self.client.get("/v1/monitors?source=marvin", headers=self.headers).json["monitors"]
            ),
            1,
        )
        self.assertEqual(
            self.client.get("/v1/monitors?source=other", headers=self.headers).status_code, 400
        )

    def test_active_monitor_cap_and_final_date_expiration(self):
        """Cap active jobs and expire final dates after local midnight."""
        store = self.app.extensions["monitor_store"]
        now = datetime(2026, 10, 3, 19, 59, tzinfo=timezone.utc)
        dated = validate_request({"locations": [LOCATION], "end_date": "2026-10-03"})
        monitor, _ = store.create(dated, "dated", now)
        self.assertIsNotNone(store.claim_due(now))
        store.finish(monitor["id"], result={"recommendation": "Waiting", "ranked_nights": []})
        store.claim_due(now + timedelta(minutes=2))
        self.assertEqual(store.get(monitor["id"])["status"], "completed")
        payload = validate_request({"locations": [LOCATION]})
        for index in range(50):
            store.create(payload, f"limit-{index}", now)
        with self.assertRaises(MonitorError) as caught:
            store.create(payload, "limit-overflow", now)
        self.assertEqual(caught.exception.status, 409)

    def test_missing_configuration_fails_closed(self):
        """Prevent the service from starting with an empty API token."""
        with self.assertRaises(RuntimeError):
            create_app({**self.config, "WEATHER_API_TOKEN": ""})

    def test_labels_reject_slack_mentions_and_control_characters(self):
        """Prevent model-provided location names from introducing Slack mentions."""
        for label in ("<!channel>", "Name\nNew line", "Name\x00", "Name\u200bHidden"):
            with self.subTest(label=label):
                response = self.create({"locations": [{**LOCATION, "name": label}]})
                self.assertEqual(response.status_code, 400)
        for channel in ("C12345678", "D12345678", "G12345678"):
            response = self.create({"locations": [LOCATION], "channel_id": channel}, key=channel)
            self.assertEqual(response.status_code, 201)

    def test_sheet_requires_slack_delivery_configuration_at_startup(self):
        """Expose missing variable names before enabling the persistent Sheet schedule."""
        for name in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID"):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, name):
                create_app({**self.config, name: ""})
        create_app(
            {
                **self.config,
                "WEATHER_SHEET_ENABLED": False,
                "SLACK_BOT_TOKEN": "",
                "SLACK_CHANNEL_ID": "",
            }
        )


if __name__ == "__main__":
    unittest.main()
