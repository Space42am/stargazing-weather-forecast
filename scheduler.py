"""Dispatch durable monitor jobs serially into cancellable child processes."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from monitoring import MonitorStore

LOGGER = logging.getLogger(__name__)


class Scheduler:
    """Run one dispatcher per database using a process-lifetime filesystem lock."""

    def __init__(self, store: MonitorStore, poll_seconds: float = 0.25):
        """Initialize a dispatcher without starting external work.

        Parameters
        ----------
        store : MonitorStore
            Durable monitor storage.
        poll_seconds : float, optional
            Cancellation and due-job polling interval.
        """
        self.store = store
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread = None
        self._lock_file = None
        self._process_lock = threading.Lock()
        self._process = None
        self._monitor_id = None
        self._heartbeat = 0.0

    def start(self) -> bool:
        """Acquire exclusive dispatch ownership and start the background thread."""
        if self._thread and self._thread.is_alive():
            return True
        self._lock_file = open(self.store.path + ".scheduler.lock", "a", encoding="utf-8")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            self._lock_file = None
            return False
        self.store.recover_interrupted()
        self._stop.clear()
        self._heartbeat = time.monotonic()
        self._thread = threading.Thread(
            target=self._dispatch, name="weather-dispatcher", daemon=True
        )
        self._thread.start()
        return True

    def healthy(self) -> bool:
        """Return whether the owner dispatcher is alive and polling recently."""
        return bool(
            self._thread and self._thread.is_alive() and time.monotonic() - self._heartbeat < 30
        )

    def shutdown(self) -> None:
        """Stop dispatching and terminate current work before releasing ownership."""
        self._stop.set()
        with self._process_lock:
            if self._process is not None:
                self._terminate(self._process)
        if self._thread:
            self._thread.join(timeout=10)
        if self._lock_file and (not self._thread or not self._thread.is_alive()):
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    def cancel(self, monitor_id: str) -> bool:
        """Terminate an owned child and wait briefly for its durable acknowledgement.

        Parameters
        ----------
        monitor_id : str
            Already stopped monitor identifier.

        Returns
        -------
        bool
            Whether no child remains in a running state.
        """
        with self._process_lock:
            if self._monitor_id == monitor_id and self._process is not None:
                self._terminate(self._process)
        deadline = time.monotonic() + 5
        while (
            self.store.get(monitor_id)["last_status"] == "running" and time.monotonic() < deadline
        ):
            time.sleep(self.poll_seconds)
        return self.store.get(monitor_id)["last_status"] != "running"

    def _terminate(self, process: subprocess.Popen) -> None:
        """Terminate the child's process group and escalate only when it stays alive.

        Parameters
        ----------
        process : subprocess.Popen
            Owned process launched in a new session.
        """
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            except ProcessLookupError:
                return

    def _dispatch(self) -> None:
        """Claim and execute due jobs while keeping the heartbeat and lock live."""
        while not self._stop.is_set():
            self._heartbeat = time.monotonic()
            monitor = None
            try:
                monitor = self.store.claim_due()
                if monitor:
                    self._run(monitor)
            except Exception as error:
                LOGGER.exception("Weather dispatcher iteration failed")
                with self._process_lock:
                    if self._process is not None:
                        self._terminate(self._process)
                    self._process = None
                    self._monitor_id = None
                if monitor:
                    self.store.finish(
                        monitor["id"],
                        error=f"Report dispatch failed ({type(error).__name__}); delivery may have occurred.",
                    )
            self._stop.wait(self.poll_seconds)

    def _run(self, monitor: dict) -> None:
        """Run a single monitor with a bounded result file and durable cancellation.

        Parameters
        ----------
        monitor : dict
            Atomically claimed monitor record.
        """
        monitor_id = monitor["id"]
        with tempfile.TemporaryDirectory(prefix="weather-result-") as directory:
            output = Path(directory) / "result.json"
            with self._process_lock:
                if self._stop.is_set() or self.store.get(monitor_id)["status"] != "active":
                    self.store.finish(monitor_id, error="Run cancelled before delivery.")
                    return
                self._process = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("run_monitor.py")),
                        self.store.path,
                        monitor_id,
                        str(output),
                        self.store.timezone.key,
                        str(os.getpid()),
                    ],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._monitor_id = monitor_id
                process = self._process
            started = time.monotonic()
            while process.poll() is None:
                self._heartbeat = time.monotonic()
                current = self.store.get(monitor_id)
                expired = (
                    current["end_date"]
                    and current["end_date"] < datetime.now(self.store.timezone).date().isoformat()
                )
                if (
                    self._stop.is_set()
                    or current["status"] != "active"
                    or expired
                    or time.monotonic() - started > 1800
                ):
                    with self._process_lock:
                        self._terminate(process)
                    break
                self._stop.wait(self.poll_seconds)
            with self._process_lock:
                self._process = None
                self._monitor_id = None
            if self._stop.is_set():
                self.store.finish(
                    monitor_id,
                    error="Run interrupted during service shutdown; delivery may have occurred.",
                )
            elif process.returncode == 0 and output.exists() and output.stat().st_size <= 512_000:
                try:
                    outcome = json.loads(output.read_text(encoding="utf-8"))
                    self.store.finish(
                        monitor_id, result=outcome["result"], error=outcome.get("error")
                    )
                except (ValueError, KeyError, TypeError):
                    self.store.finish(monitor_id, error="Report worker returned an invalid result.")
            else:
                self.store.finish(
                    monitor_id,
                    error=f"Report worker failed or was cancelled (exit {process.returncode}); delivery may have occurred.",
                )
