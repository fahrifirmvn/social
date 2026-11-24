import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from client import Task, TaskKind, Verification
from main import app
from runner import History, RetryPolicy, Runner, logger


class FakeClient:
    def __init__(self):
        self.tasks = [
            Task("1", TaskKind.FOLLOW),
            Task("1", TaskKind.FOLLOW),
            Task("2", TaskKind.LIKE),
            Task("3", TaskKind.FOLLOW),
            Task("4", TaskKind.LIKE),
        ]
        self.calls = []

    def list_tasks(self):
        return self.tasks

    def verify_task(self, task):
        self.calls.append(task.id)
        if task.id == "4":
            raise httpx.ConnectError("network unavailable")
        return {
            "1": Verification.FAILED, "2": Verification.VERIFIED, "3": Verification.PENDING
        }[task.id]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        gap = patch("runner.TASK_GAP_SECONDS", 0)
        gap.start()
        self.addCleanup(gap.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.history = History(Path(directory.name) / "data" / "history.json")
        self.client = FakeClient()
        self.runner = Runner(self.client, self.history, source="demo")
        self.progress = []

    def run_tasks(self, *, dry_run=False, watch=False, retry_policy=RetryPolicy()):
        self.runner.run(
            dry_run=dry_run, watch=watch, interval=1,
            progress=lambda *values: self.progress.append(values),
            retry_policy=retry_policy,
        )

    def test_dry_run_never_verifies_or_writes(self):
        with patch("runner.TASK_GAP_SECONDS", 3), patch("runner.time.sleep") as sleep:
            self.run_tasks(dry_run=True)
        sleep.assert_not_called()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.progress, [(4, 0, 0)])
        self.assertFalse(self.history.path.parent.exists())

    def test_instagram_only_and_three_seconds_between_final_and_next_task(self):
        self.client.tasks = [
            Task("3", TaskKind.FOLLOW, platform="tiktok"),
            Task("3", TaskKind.FOLLOW),
            Task("1", TaskKind.FOLLOW),
            Task("2", TaskKind.LIKE),
        ]
        clock = 0
        attempts = []
        verify = self.client.verify_task

        def record_attempt(task):
            attempts.append((task.id, task.platform, clock))
            return verify(task)

        def advance(seconds):
            nonlocal clock
            clock += seconds

        with (
            patch("runner.TASK_GAP_SECONDS", 3),
            patch("runner.time.monotonic", side_effect=lambda: clock),
            patch("runner.time.sleep", side_effect=advance) as sleep,
            patch.object(self.client, "verify_task", side_effect=record_attempt),
            self.assertLogs(logger, level="INFO"),
        ):
            self.run_tasks()
        self.assertEqual(attempts, [("3", "instagram", 0), ("1", "instagram", 0), ("2", "instagram", 3)])
        sleep.assert_called_once_with(3)
        self.assertEqual(self.progress[-1], (3, 1, 1))

    def test_watch_poll_interval_counts_toward_task_gap(self):
        self.client.tasks = [Task("2", TaskKind.LIKE)]
        clock = 0

        def advance(seconds):
            nonlocal clock
            clock += seconds
            if clock == 1:
                self.client.tasks.append(Task("1", TaskKind.FOLLOW))
            elif clock > 3:
                raise KeyboardInterrupt

        with (
            patch("runner.TASK_GAP_SECONDS", 3),
            patch("runner.time.monotonic", side_effect=lambda: clock),
            patch("runner.time.sleep", side_effect=advance) as sleep,
            self.assertLogs(logger, level="INFO"),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_tasks(watch=True)
        self.assertEqual(self.client.calls, ["2", "1"])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 1])

    def test_final_results_saved_in_order_and_not_retried(self):
        with self.assertLogs(logger, level="INFO"):
            self.run_tasks()
            self.run_tasks()
        self.assertEqual(self.client.calls, ["1", "2", "3", "4", "3", "4"])
        self.assertEqual(self.progress[-1], (4, 1, 1))
        self.assertEqual(
            [(row["task_id"], row["result"], row["source"]) for row in self.history.load()],
            [("1", "failed", "demo"), ("2", "verified", "demo")],
        )

    def test_watch_checks_again_after_empty_queue(self):
        self.client.tasks = []

        def next_cycle(_interval):
            if self.client.tasks:
                raise KeyboardInterrupt
            self.client.tasks = [Task("2", TaskKind.LIKE)]

        with patch("runner.time.sleep", side_effect=next_cycle), self.assertRaises(KeyboardInterrupt):
            self.run_tasks(watch=True)
        self.assertEqual(self.progress, [(0, 0, 0), (1, 0, 0), (1, 1, 0)])

    def test_history_is_scoped_to_source(self):
        self.history.append([], self.client.tasks[0], Verification.VERIFIED, "another-source")
        _, completed, failed, remaining = self.runner.snapshot()
        self.assertEqual((completed, failed, len(remaining)), (0, 0, 4))

    def test_watch_loads_history_once_and_does_not_verify_final_tasks_again(self):
        self.client.tasks = [Task("1", TaskKind.FOLLOW), Task("2", TaskKind.LIKE)]
        with (
            patch.object(self.history, "load", wraps=self.history.load) as load,
            patch("runner.time.sleep", side_effect=[None, KeyboardInterrupt]),
            self.assertLogs(logger, level="INFO"),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_tasks(watch=True)
        load.assert_called_once()
        self.assertEqual(self.client.calls, ["1", "2"])
        self.assertEqual(self.progress[-1], (2, 1, 1))

    def test_watch_retries_pending_and_transient_errors_without_blocking_new_tasks(self):
        clock = 0
        self.client.tasks = [Task("3", TaskKind.FOLLOW), Task("4", TaskKind.LIKE)]
        attempted = []
        verify = self.client.verify_task

        def record_attempt(task):
            attempted.append((task.id, clock))
            return verify(task)

        def tick(_interval):
            nonlocal clock
            clock += 1
            if clock == 1:
                self.client.tasks.append(Task("2", TaskKind.LIKE))
            if clock == 10:
                raise KeyboardInterrupt

        with (
            patch("runner.time.monotonic", side_effect=lambda: clock),
            patch("runner.time.sleep", side_effect=tick),
            patch.object(self.client, "verify_task", side_effect=record_attempt),
            self.assertLogs(logger, level="INFO") as logs,
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_tasks(watch=True, retry_policy=RetryPolicy(2, 3, 4))

        self.assertEqual([at for task_id, at in attempted if task_id == "3"], [0, 2, 5, 8])
        self.assertEqual([at for task_id, at in attempted if task_id == "4"], [0, 2, 5, 8])
        self.assertIn(("2", 1), attempted)
        self.assertTrue(any("batas 4 percobaan" in message for message in logs.output))
        self.assertEqual([row["task_id"] for row in self.history.load()], ["2"])

        self.run_tasks()
        self.assertEqual(self.client.calls[-2:], ["3", "4"])

    def test_access_denied_stops_without_finalizing_task_or_retrying(self):
        request = httpx.Request("GET", "https://example.invalid")
        denied = httpx.HTTPStatusError(
            "Forbidden", request=request, response=httpx.Response(403, request=request),
        )
        with (
            patch.object(self.client, "verify_task", side_effect=denied) as verify,
            patch("runner.time.sleep") as sleep,
            self.assertRaises(httpx.HTTPStatusError),
        ):
            self.run_tasks(watch=True)
        verify.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(self.history.load(), [])

    def test_history_lock_excludes_another_process_and_allows_readers(self):
        code = """
import sys
from pathlib import Path
from client import DemoClient
from runner import History, HistoryBusyError, Runner
try:
    Runner(DemoClient(), History(Path(sys.argv[1])), source="demo").run(
        dry_run=False, watch=False, interval=1, progress=lambda *_: None,
    )
except HistoryBusyError:
    sys.exit(7)
"""
        with self.history.write_lock():
            result = subprocess.run(
                [sys.executable, "-c", code, str(self.history.path)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 7, result.stderr)
            self.run_tasks(dry_run=True)
            self.assertEqual(self.runner.snapshot()[1:3], (0, 0))
            self.assertFalse(self.history.path.exists())

        with self.assertLogs(logger, level="INFO"):
            self.run_tasks()
        self.assertEqual(len(self.history.load()), 2)

    def test_cli_run_reports_contention(self):
        log_path = self.history.path.parent / "activity.log"
        with (
            self.history.write_lock(),
            patch("main.load_settings", return_value=(self.history, log_path, 1, RetryPolicy())),
        ):
            result = CliRunner().invoke(app, ["run", "--demo"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Runner lain sedang memakai riwayat", result.output)
        self.assertFalse(self.history.path.exists())

    def test_interrupt_before_atomic_replace_keeps_last_saved_result(self):
        self.client.tasks = [Task("1", TaskKind.FOLLOW), Task("2", TaskKind.LIKE)]
        replace_file = os.replace
        attempts = 0

        def interrupt_second_write(source, destination):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise KeyboardInterrupt
            replace_file(source, destination)

        with (
            patch("runner.os.replace", side_effect=interrupt_second_write),
            self.assertLogs(logger, level="INFO"),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_tasks()

        self.assertEqual([row["task_id"] for row in self.history.load()], ["1"])
        self.assertEqual(list(self.history.path.parent.glob("*.tmp")), [])
        self.run_tasks()
        self.assertEqual(self.client.calls, ["1", "2", "2"])
        self.assertEqual([row["task_id"] for row in self.history.load()], ["1", "2"])

    def test_cli_refuses_implicit_live_execution(self):
        result = CliRunner().invoke(app, ["run"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("Integrasi resmi belum dikonfigurasi", result.output)

    def test_cli_exits_cleanly_after_ctrl_c(self):
        log_path = self.history.path.parent / "activity.log"
        with (
            patch("main.load_settings", return_value=(self.history, log_path, 1, RetryPolicy())),
            patch("main.time.sleep", side_effect=KeyboardInterrupt),
        ):
            result = CliRunner().invoke(app, ["monitor"])
        self.assertEqual(result.exit_code, 130)
        self.assertIn("Dihentikan oleh pengguna", log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
