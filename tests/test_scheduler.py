"""Verify durable run recovery, singleton ownership and process cancellation."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from monitoring import MonitorStore, validate_request
from run_monitor import install_parent_guard, run
from scheduler import Scheduler


class SchedulerTests(unittest.TestCase):
    """Use real short-lived subprocesses without making weather or Slack requests."""

    def setUp(self):
        """Initialize an isolated store and retain the real process constructor."""
        self.directory = tempfile.TemporaryDirectory()
        self.store = MonitorStore(str(Path(self.directory.name) / "monitors.sqlite"))
        self.schedulers = []
        self.popen = subprocess.Popen
        self.payload = validate_request({"locations": [{"name": "Test", "lat": 40, "lon": 44}]})

    def tearDown(self):
        """Shut down all owned dispatcher processes and remove their databases."""
        for scheduler in self.schedulers:
            scheduler.shutdown()
        self.directory.cleanup()

    def dispatcher(self):
        """Create and retain a fast-polling test dispatcher."""
        scheduler = Scheduler(self.store, poll_seconds=0.02)
        self.schedulers.append(scheduler)
        return scheduler

    def wait_for(self, predicate, seconds=5):
        """Wait briefly for asynchronous evidence or fail with a clear timeout.

        Parameters
        ----------
        predicate : callable
            Condition indicating that the asynchronous operation finished.
        seconds : float, optional
            Maximum wait duration.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("Timed out waiting for dispatcher state")

    def test_restart_retains_schedule_and_marks_uncertain_delivery(self):
        """Avoid immediate notification replay after an interrupted in-flight run."""
        now = datetime.now(timezone.utc)
        monitor, _ = self.store.create(self.payload, "restart", now)
        claimed = self.store.claim_due(now)
        restarted = MonitorStore(self.store.path)
        restarted.recover_interrupted()
        record = restarted.get(monitor["id"])
        self.assertEqual(record["last_status"], "failed")
        self.assertIn("delivery may have occurred", record["last_error"])
        self.assertEqual(record["next_run_at"], claimed["next_run_at"])
        self.assertIsNone(restarted.claim_due(now + timedelta(seconds=1)))

    def test_singleton_dispatcher_lock_and_health(self):
        """Prevent a second process owner from dispatching duplicate jobs."""
        first, second = self.dispatcher(), self.dispatcher()
        self.assertTrue(first.start())
        self.assertFalse(second.start())
        self.assertTrue(first.healthy())
        first.shutdown()
        self.assertFalse(first.healthy())
        self.assertTrue(second.start())

    def test_completed_child_records_only_bounded_summary(self):
        """Capture the public result while dropping report payload internals."""
        monitor, _ = self.store.create(self.payload, "success")

        def worker(command, **kwargs):
            """Replace the real weather worker with a harmless result-writing child.

            Parameters
            ----------
            command : list[str]
                Dispatcher worker command.
            **kwargs : dict
                Owned-session process options.

            Returns
            -------
            Popen
                Harmless result-writing process.
            """
            content = json.dumps(
                {"result": {"recommendation": "Clear", "ranked_nights": [], "reports": ["private"]}}
            )
            code = (
                "from pathlib import Path; Path("
                + repr(command[4])
                + ").write_text("
                + repr(content)
                + ")"
            )
            return self.popen([command[0], "-c", code], **kwargs)

        scheduler = self.dispatcher()
        with patch("scheduler.subprocess.Popen", side_effect=worker):
            scheduler.start()
            self.wait_for(lambda: self.store.get(monitor["id"])["last_status"] == "succeeded")
        record = self.store.get(monitor["id"])
        self.assertEqual(record["latest_result"], {"recommendation": "Clear", "ranked_nights": []})

    def test_stop_kills_only_the_owned_process_before_receipt(self):
        """Terminate the active Marvin child without changing a Sheet-owned record."""
        self.store.configure_sheet(True)
        monitor, _ = self.store.create(self.payload, "cancel")
        processes = []

        def worker(command, **kwargs):
            """Start a harmless blocking child so cancellation can be observed.

            Parameters
            ----------
            command : list[str]
                Dispatcher worker command.
            **kwargs : dict
                Owned-session process options.

            Returns
            -------
            Popen
                Harmless blocking process.
            """
            process = self.popen([command[0], "-c", "import time; time.sleep(60)"], **kwargs)
            processes.append(process)
            return process

        scheduler = self.dispatcher()
        with patch("scheduler.subprocess.Popen", side_effect=worker):
            scheduler.start()
            self.wait_for(lambda: bool(processes))
            self.store.stop(monitor["id"])
            self.assertTrue(scheduler.cancel(monitor["id"]))
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(self.store.get(monitor["id"])["last_status"], "stopped")
        self.assertEqual(self.store.get("sheet")["status"], "active")

    def test_sheet_schedule_uses_yerevan_nine_daily(self):
        """Preserve the daily wall-clock Sheet cadence independently of Marvin jobs."""
        before = datetime(2026, 10, 3, 4, 59, tzinfo=timezone.utc)
        self.store.configure_sheet(True, before)
        self.assertEqual(self.store.get("sheet")["next_run_at"], "2026-10-03T05:00:00Z")
        sheet = self.store.claim_due(before + timedelta(minutes=1))
        self.assertEqual(sheet["id"], "sheet")
        self.assertEqual(sheet["next_run_at"], "2026-10-04T05:00:00Z")

    def test_worker_routes_only_its_monitor_and_treats_empty_horizon_as_success(self):
        """Forward event dates without substituting Sheet locations or failing an empty horizon."""
        payload = validate_request(
            {
                **self.payload,
                "start_date": "2099-01-01",
                "end_date": "2099-01-03",
                "channel_id": "C12345678",
            }
        )
        monitor, _ = self.store.create(payload, "future-event")
        output = str(Path(self.directory.name) / "result.json")
        explicit = Mock(
            return_value={
                "recommendation": "Event dates are outside the available forecast.",
                "ranked_nights": [],
            }
        )
        sheet = Mock()
        with patch.dict(
            "sys.modules",
            {"pipeline": SimpleNamespace(execute_report=explicit, execute_sheet_report=sheet)},
        ):
            run(self.store.path, monitor["id"], output, "Asia/Yerevan")
        self.assertEqual(explicit.call_args.args[0], payload["locations"])
        self.assertEqual(explicit.call_args.kwargs["start_date"], "2099-01-01")
        self.assertEqual(explicit.call_args.kwargs["channel_id"], "C12345678")
        sheet.assert_not_called()
        outcome = json.loads(Path(output).read_text())
        self.assertIsNone(outcome.get("error"))
        self.assertEqual(outcome["result"]["ranked_nights"], [])

    def test_spawn_failure_marks_job_failed_and_keeps_scheduler_healthy(self):
        """Release a claimed job when worker launch fails before creating a process."""
        monitor, _ = self.store.create(self.payload, "spawn-failure")
        scheduler = self.dispatcher()
        with (
            patch("scheduler.subprocess.Popen", side_effect=OSError("worker unavailable")),
            self.assertLogs("scheduler", level="ERROR"),
        ):
            scheduler.start()
            self.wait_for(lambda: self.store.get(monitor["id"])["last_status"] == "failed")
        self.assertTrue(scheduler.healthy())
        self.assertIsNotNone(self.store.get(monitor["id"])["next_run_at"])

    def test_parent_exit_terminates_its_owned_worker(self):
        """Kill an orphan worker when its parent exits abruptly without cleanup."""
        marker = str(Path(self.directory.name) / "guard-ready")
        child_code = (
            "import json,os,subprocess,sys,time; from pathlib import Path; from run_monitor import install_parent_guard; "
            "install_parent_guard(os.getppid()); child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path("
            + repr(marker)
            + ").write_text(json.dumps([os.getpid(),child.pid])); time.sleep(60)"
        )
        parent_code = (
            "import os,subprocess,sys,time; from pathlib import Path; "
            "subprocess.Popen([sys.executable,'-c',"
            + repr(child_code)
            + "],start_new_session=True); "
            "deadline=time.monotonic()+5\n"
            "while not Path("
            + repr(marker)
            + ").exists() and time.monotonic()<deadline: time.sleep(0.02)\n"
            "os._exit(0 if Path(" + repr(marker) + ").exists() else 1)"
        )
        parent = self.popen(
            [sys.executable, "-c", parent_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(parent.wait(timeout=8), 0)
        worker_pid, descendant_pid = json.loads(Path(marker).read_text())

        def exited():
            """Return whether the orphan exited or awaits its init process's reaping."""
            for pid in (worker_pid, descendant_pid):
                state = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "stat="],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
                if state and not state.startswith("Z"):
                    return False
            return True

        try:
            self.wait_for(exited)
        finally:
            if not exited():
                os.killpg(worker_pid, signal.SIGKILL)

    def test_linux_guard_registers_parent_death_signal(self):
        """Register Linux parent-death protection before starting the portable watcher."""
        library = Mock()
        library.prctl.return_value = 0
        with (
            patch("run_monitor.sys.platform", "linux"),
            patch("run_monitor.os.getpgrp", return_value=123),
            patch("run_monitor.os.getpid", return_value=123),
            patch("run_monitor.os.getppid", return_value=456),
            patch("run_monitor.signal.signal") as handler,
            patch("run_monitor.ctypes.CDLL", return_value=library),
            patch("run_monitor.threading.Thread") as thread,
        ):
            install_parent_guard(456)
        library.prctl.assert_called_once_with(1, signal.SIGTERM, 0, 0, 0)
        handler.assert_called_once()
        thread.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
