import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from follow_workflow import FollowTask, FollowView, read_checkpoint, run_follow_demo, work_one
from main import app
from runner import History, RetryPolicy


class FollowWorkflowTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "checkpoint.json"
        self.view = Mock(spec=FollowView)
        self.view.task.return_value = FollowTask("1", "fixture_user", 1)
        self.view.profile.side_effect = [
            ("fixture_user", False), ("fixture_user", True), ("fixture_user", True),
        ]
        self.view.submit_delay.return_value = 0
        self.view.status.return_value = "queued"

    def test_queue_checkpoint_prevents_second_attempt(self):
        checkpoint = work_one(self.view, self.path)
        self.assertEqual(checkpoint.phase, "queued")
        self.assertEqual(work_one(self.view, self.path), checkpoint)
        self.view.start.assert_called_once()
        self.view.follow.assert_called_once()
        self.view.check.assert_called_once()

    def test_quota_that_disappears_before_start_is_skipped(self):
        self.view.task.side_effect = [FollowTask("1", "fixture_user", 1), FollowTask("1", "fixture_user", 0)]
        self.assertIsNone(work_one(self.view, self.path))
        self.view.start.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_mismatched_target_never_clicks_follow(self):
        self.view.profile.side_effect = [("other_user", False)]
        with self.assertRaisesRegex(ValueError, "Target tidak cocok"):
            work_one(self.view, self.path)
        self.view.follow.assert_not_called()
        self.view.check.assert_not_called()

    def test_interruption_at_follow_keeps_uncertain_stage_and_never_repeats_click(self):
        self.view.follow.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            work_one(self.view, self.path)
        self.assertEqual(read_checkpoint(self.path).phase, "following")
        work_one(self.view, self.path)
        self.view.follow.assert_called_once()
        self.view.check.assert_not_called()

    def test_already_following_skips_follow_and_respects_submit_delay(self):
        self.view.profile.side_effect = None
        self.view.profile.return_value = ("fixture_user", True)
        self.view.submit_delay.return_value = 3
        self.view.status.return_value = "verified"
        with (
            patch("follow_workflow.time.monotonic", side_effect=[10, 11]),
            patch("follow_workflow.time.sleep") as sleep,
        ):
            self.assertEqual(work_one(self.view, self.path).phase, "verified")
        sleep.assert_called_once_with(2)
        self.view.follow.assert_not_called()
        self.view.check.assert_called_once()

    def test_unknown_verification_never_marks_final(self):
        self.view.status.return_value = "unexpected"
        with self.assertRaisesRegex(ValueError, "tidak dikenali"):
            work_one(self.view, self.path)
        self.assertEqual(read_checkpoint(self.path).phase, "checking")

    def test_cli_live_once_invokes_executor_with_single_task_limit(self):
        # Mode live sudah diimplementasikan: run --live --once harus memanggil
        # executor sekali dengan batas 1 task, default background, tanpa membuka
        # browser sungguhan (run_live_tasks di-patch).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = (History(root / "history.json"), root / "activity.log", 1, RetryPolicy())
            with (
                patch("main.load_settings", return_value=settings),
                patch("task_executor.get_daily_count", return_value=0),
                patch("task_executor.run_live_tasks", return_value=[]) as run_live,
            ):
                result = CliRunner().invoke(app, ["run", "--live", "--once"])
        self.assertEqual(result.exit_code, 0, result.output)
        run_live.assert_called_once()
        self.assertEqual(run_live.call_args.kwargs.get("max_tasks"), 1)
        self.assertTrue(run_live.call_args.kwargs.get("background"))

    def test_playwright_fixture_completes_click_sequence_offline(self):
        try:
            import playwright.sync_api
        except ImportError:
            self.skipTest("Playwright opsional belum terpasang")
        try:
            checkpoint, resumed = run_follow_demo(self.path)
        except ValueError as exc:
            if "playwright install chromium" in str(exc):
                self.skipTest(str(exc))
            raise
        self.assertFalse(resumed)
        self.assertEqual(checkpoint.phase, "queued")
        with patch("follow_workflow.open_fixture_page") as browser:
            self.assertEqual(run_follow_demo(self.path), (checkpoint, True))
        browser.assert_not_called()
