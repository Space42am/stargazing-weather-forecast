"""Validate monitor requests and persist their lifecycle in SQLite."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


class MonitorError(ValueError):
    """Represent a safe validation or lifecycle error for API clients."""

    def __init__(self, message: str, status: int = 400):
        """Initialize an error with its HTTP status.

        Parameters
        ----------
        message : str
            Public error description.
        status : int, optional
            HTTP status for the response.
        """
        super().__init__(message)
        self.status = status


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time.

    Returns
    -------
    datetime
        Current UTC time.
    """
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    """Serialize a timezone-aware time as an ISO UTC timestamp.

    Parameters
    ----------
    value : datetime
        Time to normalize to UTC.

    Returns
    -------
    str
        ISO timestamp with a UTC suffix.
    """
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_request(body: object) -> dict:
    """Validate and normalize a Marvin monitor request.

    Parameters
    ----------
    body : object
        Decoded JSON request body.

    Returns
    -------
    dict
        Normalized locations and monitoring options.

    Raises
    ------
    MonitorError
        If the request contains unsupported or invalid fields.
    """
    allowed = {"locations", "interval_seconds", "start_date", "end_date", "channel_id"}
    if not isinstance(body, dict) or set(body) - allowed:
        raise MonitorError(
            "Use only locations, interval_seconds, start_date, end_date and channel_id."
        )
    locations = body.get("locations")
    if not isinstance(locations, list) or not 1 <= len(locations) <= 10:
        raise MonitorError("Provide between 1 and 10 locations.")
    normalized = []
    for location in locations:
        if not isinstance(location, dict) or set(location) != {"name", "lat", "lon"}:
            raise MonitorError("Each location requires only name, lat and lon.")
        name = location["name"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise MonitorError("Location names must contain between 1 and 120 characters.")
        if any(character in "<>" or not character.isprintable() for character in name):
            raise MonitorError(
                "Location names cannot contain angle brackets or non-printable characters."
            )
        coordinates = {}
        for key, limit in (("lat", 90), ("lon", 180)):
            value = location[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or abs(value) > limit
                or not math.isfinite(value)
            ):
                raise MonitorError(
                    f"Location {key} must be a finite number between {-limit} and {limit}."
                )
            coordinates[key] = float(value)
        normalized.append({"name": name.strip(), **coordinates})
    interval = body.get("interval_seconds", 86400)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 3600 <= interval <= 604800
    ):
        raise MonitorError("interval_seconds must be an integer between 3600 and 604800.")
    dates = {}
    for key in ("start_date", "end_date"):
        value = body.get(key)
        if value is not None:
            try:
                if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError
                date.fromisoformat(value)
            except ValueError as exc:
                raise MonitorError(f"{key} must be an ISO date (YYYY-MM-DD).") from exc
        dates[key] = value
    if dates["start_date"] and dates["end_date"] and dates["end_date"] < dates["start_date"]:
        raise MonitorError("end_date must be on or after start_date.")
    channel = body.get("channel_id")
    if channel is not None and (
        not isinstance(channel, str) or not re.fullmatch(r"[CDG][A-Z0-9]{8,31}", channel)
    ):
        raise MonitorError("channel_id must be a valid Slack channel ID.")
    return {"locations": normalized, "interval_seconds": interval, **dates, "channel_id": channel}


class MonitorStore:
    """Persist monitors and atomic run claims using short SQLite transactions."""

    def __init__(self, path: str, timezone_name: str = "Asia/Yerevan", sheet_hour: int = 9):
        """Initialize the durable store.

        Parameters
        ----------
        path : str
            SQLite database file path.
        timezone_name : str, optional
            Timezone for Sheet schedules and event-date expiration.
        sheet_hour : int, optional
            Local hour for the daily Sheet report.
        """
        self.path = str(Path(path).resolve())
        self.timezone = ZoneInfo(timezone_name)
        self.sheet_hour = sheet_hour
        if not 0 <= sheet_hour <= 23:
            raise ValueError("WEATHER_SHEET_HOUR must be between 0 and 23.")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS monitors (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL,
                locations TEXT NOT NULL, interval_seconds INTEGER NOT NULL,
                start_date TEXT, end_date TEXT, channel_id TEXT, created_at TEXT NOT NULL,
                next_run_at TEXT, last_run_at TEXT, last_status TEXT NOT NULL,
                last_error TEXT, latest_result TEXT, idempotency_key TEXT UNIQUE,
                payload_hash TEXT)""")

    @contextmanager
    def connect(self):
        """Yield a transaction connection and close it after committing or rolling back."""
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _record(self, row: sqlite3.Row) -> dict:
        """Decode public monitor fields without exposing idempotency metadata.

        Parameters
        ----------
        row : sqlite3.Row
            Persisted monitor fields.

        Returns
        -------
        dict
            Decoded public fields.
        """
        record = dict(row)
        for key in ("locations", "latest_result"):
            record[key] = json.loads(record[key]) if record[key] else None
        record.pop("idempotency_key")
        record.pop("payload_hash")
        return record

    def get(self, monitor_id: str) -> dict:
        """Return a monitor or raise a public not-found error.

        Parameters
        ----------
        monitor_id : str
            Durable monitor identifier.

        Returns
        -------
        dict
            Persisted public monitor record.
        """
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
        if row is None:
            raise MonitorError("Monitor not found.", 404)
        return self._record(row)

    def list(self, source: str | None = None) -> list[dict]:
        """Return monitors, optionally filtered by their ownership source.

        Parameters
        ----------
        source : str, optional
            Ownership filter, either ``marvin`` or ``sheet``.

        Returns
        -------
        list[dict]
            Public records matching the ownership filter.
        """
        if source not in (None, "marvin", "sheet"):
            raise MonitorError("source must be marvin or sheet.")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM monitors WHERE (? IS NULL OR source=?) ORDER BY created_at, id",
                (source, source),
            ).fetchall()
        return [self._record(row) for row in rows]

    def create(self, payload: dict, key: str, now: datetime | None = None) -> tuple[dict, bool]:
        """Create an immediately due Marvin monitor or return its idempotent replay.

        Parameters
        ----------
        payload : dict
            Validated monitor request.
        key : str
            Stable idempotency key for one requested monitor.
        now : datetime, optional
            UTC clock override.

        Returns
        -------
        tuple[dict, bool]
            Public monitor and whether it was newly created.
        """
        if not key or len(key) > 128:
            raise MonitorError("Idempotency-Key must contain between 1 and 128 characters.")
        now = now or utc_now()
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM monitors WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                if existing["payload_hash"] != digest:
                    raise MonitorError(
                        "Idempotency-Key was already used with a different request.", 409
                    )
                return self._record(existing), False
            self._expire(connection, now)
            active = connection.execute(
                "SELECT COUNT(*) FROM monitors WHERE source='marvin' AND status='active'"
            ).fetchone()[0]
            if active >= 50:
                raise MonitorError("The limit of 50 active Marvin monitors has been reached.", 409)
            expired = (
                payload["end_date"]
                and payload["end_date"] < now.astimezone(self.timezone).date().isoformat()
            )
            monitor_id = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO monitors (
                id,source,status,locations,interval_seconds,start_date,end_date,channel_id,
                created_at,next_run_at,last_status,idempotency_key,payload_hash)
                VALUES (?,'marvin',?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    monitor_id,
                    "completed" if expired else "active",
                    json.dumps(payload["locations"]),
                    payload["interval_seconds"],
                    payload["start_date"],
                    payload["end_date"],
                    payload["channel_id"],
                    timestamp(now),
                    None if expired else timestamp(now),
                    "pending",
                    key,
                    digest,
                ),
            )
        return self.get(monitor_id), True

    def next_sheet_run(self, now: datetime) -> str:
        """Return the next daily Sheet run in UTC from a local wall-clock hour.

        Parameters
        ----------
        now : datetime
            Current timezone-aware time.

        Returns
        -------
        str
            Next scheduled Sheet run as an ISO UTC timestamp.
        """
        local = now.astimezone(self.timezone)
        following = local.replace(hour=self.sheet_hour, minute=0, second=0, microsecond=0)
        if following <= local:
            following += timedelta(days=1)
        return timestamp(following)

    def configure_sheet(self, enabled: bool, now: datetime | None = None) -> None:
        """Create or enable the stable Sheet-owned monitor without fetching locations.

        Parameters
        ----------
        enabled : bool
            Whether the daily Sheet schedule should run.
        now : datetime, optional
            UTC clock override.
        """
        now = now or utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO monitors
                (id,source,status,locations,interval_seconds,created_at,next_run_at,last_status)
                VALUES ('sheet','sheet',?,'[]',86400,?,?,'pending')
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                next_run_at=CASE WHEN monitors.status != excluded.status THEN excluded.next_run_at
                ELSE monitors.next_run_at END""",
                (
                    "active" if enabled else "stopped",
                    timestamp(now),
                    self.next_sheet_run(now) if enabled else None,
                ),
            )

    def stop(self, monitor_id: str) -> dict:
        """Durably stop only a Marvin-owned monitor before process cancellation.

        Parameters
        ----------
        monitor_id : str
            Identifier whose ownership must be Marvin.

        Returns
        -------
        dict
            Public record after the durable stop request.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)).fetchone()
            if row is None:
                raise MonitorError("Monitor not found.", 404)
            if row["source"] != "marvin":
                raise MonitorError("Sheet-owned monitoring cannot be stopped by Marvin.", 403)
            if row["status"] == "active":
                connection.execute(
                    """UPDATE monitors SET status='stopped',next_run_at=NULL,
                    last_status=CASE WHEN last_status='running' THEN 'running' ELSE 'stopped' END
                    WHERE id=?""",
                    (monitor_id,),
                )
        return self.get(monitor_id)

    def _expire(self, connection: sqlite3.Connection, now: datetime) -> None:
        """Expire monitors after their final event date in the configured timezone.

        Parameters
        ----------
        connection : sqlite3.Connection
            Open transaction for atomic expiration.
        now : datetime
            Current timezone-aware time.
        """
        connection.execute(
            """UPDATE monitors SET status='completed',next_run_at=NULL
            WHERE source='marvin' AND status='active' AND end_date IS NOT NULL AND end_date < ?""",
            (now.astimezone(self.timezone).date().isoformat(),),
        )

    def claim_due(self, now: datetime | None = None) -> dict | None:
        """Atomically claim one due job and schedule its next interval before delivery.

        Parameters
        ----------
        now : datetime, optional
            UTC clock override.

        Returns
        -------
        dict or None
            Claimed monitor or no record when nothing is due.
        """
        now = now or utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, now)
            row = connection.execute(
                """SELECT * FROM monitors WHERE status='active'
                AND last_status != 'running' AND next_run_at <= ? ORDER BY next_run_at,id LIMIT 1""",
                (timestamp(now),),
            ).fetchone()
            if row is None:
                return None
            following = (
                self.next_sheet_run(now)
                if row["source"] == "sheet"
                else timestamp(now + timedelta(seconds=row["interval_seconds"]))
            )
            connection.execute(
                """UPDATE monitors SET last_status='running',last_run_at=?,
                next_run_at=?,last_error=NULL WHERE id=?""",
                (timestamp(now), following, row["id"]),
            )
        return self.get(row["id"])

    def finish(self, monitor_id: str, result: dict | None = None, error: str | None = None) -> None:
        """Record a bounded report result or safe failure while preserving stop ownership.

        Parameters
        ----------
        monitor_id : str
            Completed run's durable identifier.
        result : dict, optional
            Recommendation and ranked nights returned by the report pipeline.
        error : str, optional
            Safe failure summary that contains no provider response or credentials.
        """
        latest = None
        if result is not None:
            latest = json.dumps(
                {
                    "recommendation": result.get("recommendation", ""),
                    "ranked_nights": result.get("ranked_nights", []),
                },
                default=str,
            )
            if len(latest.encode()) > 512_000:
                latest = None
                error = "Report result exceeded the storage limit."
        with self.connect() as connection:
            self._expire(connection, utc_now())
            connection.execute(
                """UPDATE monitors SET last_status=CASE
                WHEN status = 'stopped' THEN 'stopped' WHEN ? IS NULL THEN 'succeeded' ELSE 'failed' END,
                last_error=?,latest_result=COALESCE(?,latest_result) WHERE id=?""",
                (error, error, latest, monitor_id),
            )

    def recover_interrupted(self) -> None:
        """Mark interrupted deliveries uncertain without immediately replaying notifications."""
        with self.connect() as connection:
            connection.execute("""UPDATE monitors SET last_status=CASE WHEN status='stopped'
                THEN 'stopped' ELSE 'failed' END,
                last_error='Run interrupted; delivery may have occurred. Next scheduled run retained.'
                WHERE last_status='running'""")
