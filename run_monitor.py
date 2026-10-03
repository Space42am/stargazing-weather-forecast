"""Execute exactly one owned report in a cancellable scheduler subprocess."""

import ctypes
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from monitoring import MonitorStore


def install_parent_guard(expected_parent_pid: int) -> None:
    """Terminate an owned report session when its dispatcher process dies.

    Parameters
    ----------
    expected_parent_pid : int
        Dispatcher PID captured before spawning this worker.
    """
    if os.getpgrp() != os.getpid():
        raise RuntimeError("Report worker requires its own process session.")

    def terminate_group(signum, frame):
        """Terminate the entire owned session with the default signal action.

        Parameters
        ----------
        signum : int
            Received termination signal.
        frame : frame or None
            Interrupted interpreter frame.
        """
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.killpg(os.getpgrp(), signal.SIGTERM)
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, terminate_group)
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise RuntimeError("Unable to configure report parent-death protection.")
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)

    def watch_parent():
        """Detect parent death on platforms without Linux process-death signals."""
        while True:
            if os.getppid() != expected_parent_pid:
                os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(0.25)

    threading.Thread(target=watch_parent, name="weather-parent-guard", daemon=True).start()


def run(
    database: str,
    monitor_id: str,
    output_path: str,
    timezone_name: str,
    expected_parent_pid: int | str | None = None,
) -> None:
    """Execute a monitor and write its bounded public result.

    Parameters
    ----------
    database : str
        Durable monitor database path.
    monitor_id : str
        Single monitor claimed by the parent dispatcher.
    output_path : str
        Temporary result path controlled by the parent process.
    timezone_name : str
        Event-date expiration timezone.
    expected_parent_pid : int or str, optional
        Parent dispatcher PID; supplied for owned subprocess execution.
    """
    if expected_parent_pid is not None:
        expected_parent_pid = int(expected_parent_pid)
        install_parent_guard(expected_parent_pid)
    store = MonitorStore(database, timezone_name)
    monitor = store.get(monitor_id)

    def cancelled() -> bool:
        """Return whether ownership, date expiry or a stop prevents delivery."""
        current = store.get(monitor_id)
        return (
            (expected_parent_pid is not None and os.getppid() != expected_parent_pid)
            or current["status"] != "active"
            or bool(
                current["end_date"]
                and current["end_date"] < datetime.now(store.timezone).date().isoformat()
            )
        )

    if cancelled():
        return
    from pipeline import execute_report, execute_sheet_report

    try:
        if monitor["source"] == "sheet":
            result = execute_sheet_report(channel_id=monitor["channel_id"], cancelled=cancelled)
        else:
            result = execute_report(
                monitor["locations"],
                channel_id=monitor["channel_id"],
                start_date=monitor["start_date"],
                end_date=monitor["end_date"],
                cancelled=cancelled,
            )
    except Exception as error:
        if not cancelled():
            Path(output_path).write_text(
                json.dumps(
                    {"result": None, "error": f"Report pipeline failed ({type(error).__name__})."}
                ),
                encoding="utf-8",
            )
        return
    if cancelled():
        return
    bounded = {
        "recommendation": result.get("recommendation", ""),
        "ranked_nights": result.get("ranked_nights", []),
    }
    Path(output_path).write_text(json.dumps({"result": bounded}, default=str), encoding="utf-8")


if __name__ == "__main__":
    run(*sys.argv[1:])
