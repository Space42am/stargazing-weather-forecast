"""Expose authenticated monitor lifecycle endpoints without startup weather I/O."""

import atexit
import hmac
import os

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from monitoring import MonitorError, MonitorStore, validate_request
from scheduler import Scheduler


def create_app(config: dict | None = None) -> Flask:
    """Create the single-worker weather API and optionally start its dispatcher.

    Parameters
    ----------
    config : dict, optional
        Flask configuration overrides, primarily for isolated tests.

    Returns
    -------
    Flask
        Authenticated monitor application.
    """
    app = Flask(__name__)
    app.config.from_mapping(
        WEATHER_API_TOKEN=os.environ.get("WEATHER_API_TOKEN", ""),
        WEATHER_DB_PATH=os.environ.get("WEATHER_DB_PATH", "data/monitors.sqlite"),
        WEATHER_SHEET_ENABLED=os.environ.get("WEATHER_SHEET_ENABLED", "true").lower()
        in {"1", "true", "yes"},
        WEATHER_TIMEZONE=os.environ.get("WEATHER_TIMEZONE", "Asia/Yerevan"),
        WEATHER_SHEET_HOUR=int(os.environ.get("WEATHER_SHEET_HOUR", "9")),
        START_SCHEDULER=True,
        SLACK_BOT_TOKEN=os.environ.get("SLACK_BOT_TOKEN", ""),
        SLACK_CHANNEL_ID=os.environ.get("SLACK_CHANNEL_ID", ""),
        MAX_CONTENT_LENGTH=32_768,
    )
    if config:
        app.config.update(config)
    if not app.config["WEATHER_API_TOKEN"]:
        raise RuntimeError("WEATHER_API_TOKEN must be configured.")
    if app.config["WEATHER_SHEET_ENABLED"]:
        missing = [name for name in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID") if not app.config[name]]
        if missing:
            raise RuntimeError(
                "Sheet monitoring requires configuration: " + ", ".join(missing) + "."
            )
    store = MonitorStore(
        app.config["WEATHER_DB_PATH"],
        app.config["WEATHER_TIMEZONE"],
        app.config["WEATHER_SHEET_HOUR"],
    )
    scheduler = Scheduler(store)
    app.extensions.update(monitor_store=store, weather_scheduler=scheduler)
    if app.config["START_SCHEDULER"]:
        if not scheduler.start():
            raise RuntimeError(
                "A weather dispatcher already owns this database. Run one API worker."
            )
        atexit.register(scheduler.shutdown)
    store.configure_sheet(app.config["WEATHER_SHEET_ENABLED"])

    @app.before_request
    def authorize():
        """Require constant-time bearer-token comparison for every versioned route."""
        if request.path == "/v1" or request.path.startswith("/v1/"):
            supplied = request.headers.get("Authorization", "")
            expected = "Bearer " + app.config["WEATHER_API_TOKEN"]
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                return jsonify(error="Unauthorized."), 401

    @app.errorhandler(MonitorError)
    def monitor_error(error):
        """Return safe monitor validation and lifecycle errors as JSON.

        Parameters
        ----------
        error : MonitorError
            Public validation or lifecycle failure.

        Returns
        -------
        tuple
            JSON error response and HTTP status.
        """
        return jsonify(error=str(error)), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        """Return bounded HTTP parser errors without reflecting request contents.

        Parameters
        ----------
        error : HTTPException
            Framework parsing or routing failure.

        Returns
        -------
        tuple
            JSON error response and HTTP status.
        """
        return jsonify(error=error.name), error.code

    @app.get("/healthz")
    def health():
        """Expose basic health without monitor data or authentication material."""
        healthy = scheduler.healthy() if app.config["START_SCHEDULER"] else True
        return jsonify(
            status="ok" if healthy else "unhealthy", scheduler=healthy
        ), 200 if healthy else 503

    @app.get("/v1/monitors")
    def list_monitors():
        """Return monitor records with optional ownership filtering."""
        return jsonify(monitors=store.list(request.args.get("source")))

    @app.post("/v1/monitors")
    def create_monitor():
        """Create only Marvin-owned monitoring with required idempotency semantics."""
        payload = validate_request(request.get_json())
        monitor, created = store.create(payload, request.headers.get("Idempotency-Key", "").strip())
        return jsonify(monitor), 201 if created else 200

    @app.get("/v1/monitors/<monitor_id>")
    def get_monitor(monitor_id):
        """Return one monitor's durable lifecycle and latest report.

        Parameters
        ----------
        monitor_id : str
            Persisted monitor identifier.

        Returns
        -------
        Response
            JSON public monitor record.
        """
        return jsonify(store.get(monitor_id))

    @app.post("/v1/monitors/<monitor_id>/stop")
    def stop_monitor(monitor_id):
        """Stop Marvin-owned work durably and acknowledge process cancellation.

        Parameters
        ----------
        monitor_id : str
            Marvin-owned monitor identifier.

        Returns
        -------
        tuple
            JSON durable stop receipt and cancellation acknowledgement status.
        """
        store.stop(monitor_id)
        confirmed = scheduler.cancel(monitor_id)
        return jsonify(
            monitor=store.get(monitor_id), cancellation_confirmed=confirmed
        ), 200 if confirmed else 202

    return app
