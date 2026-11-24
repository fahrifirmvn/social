import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from client import Task, TaskKind
from fixture_monitor import FixtureError, open_fixture_page, render_fixture
from main import app
from runner import History, RetryPolicy

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class FixtureCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.path = base / "queue.html"
        self.path.touch()
        self.history_path = base / "data" / "history.json"
        self.log_path = base / "logs" / "activity.log"

    def invoke_monitor(self, results, *, once, on_sleep=None):
        @contextmanager
        def fake_browser():
            yield object()

        args = ["monitor", "--fixture", str(self.path)]
        if once:
            args.append("--once")
        with (
            patch("main.load_settings", return_value=(History(self.history_path), self.log_path, 1, RetryPolicy())),
            patch("main.open_fixture_page", fake_browser),
            patch("main.render_fixture", side_effect=results) as render,
            patch("main.time.sleep", side_effect=on_sleep or [None, KeyboardInterrupt]) as sleep,
        ):
            result = CliRunner().invoke(app, args)
        self.assertFalse(self.history_path.parent.exists())
        self.assertFalse(self.log_path.parent.exists())
        return result, render, sleep

    def test_once_reports_ready_queue_without_sleeping(self):
        self.path.write_bytes(b"one-time fixture")
        result, render, sleep = self.invoke_monitor([[Task("1", TaskKind.FOLLOW)]], once=True)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("FIXTURE LOKAL", result.output)
        self.assertEqual(render.call_args.args[1], b"one-time fixture")
        sleep.assert_not_called()

    def test_same_loading_content_can_be_rendered_again(self):
        result, render, sleep = self.invoke_monitor([None, []], once=False)
        self.assertEqual(result.exit_code, 130, result.output)
        self.assertIn("jumlah task belum diketahui", result.output)
        self.assertIn("FIXTURE LOKAL", result.output)
        self.assertEqual(render.call_count, 2)
        self.assertEqual(sleep.call_count, 2)

    def test_skips_unchanged_ready_content_and_renders_after_file_replacement(self):
        first = b"<ul data-queue-state='ready' data-revision='1'></ul>"
        second = first.replace(b"'1'", b"'2'")
        self.path.write_bytes(first)
        sleeps = 0

        def update_fixture(_interval):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                temporary = self.path.with_suffix(".tmp")
                temporary.write_bytes(second)
                temporary.replace(self.path)
            if sleeps == 4:
                raise KeyboardInterrupt

        result, render, sleep = self.invoke_monitor(
            [[], [Task("2", TaskKind.LIKE)]], once=False, on_sleep=update_fixture,
        )
        self.assertEqual(result.exit_code, 130, result.output)
        self.assertEqual([call.args[1] for call in render.call_args_list], [first, second])
        self.assertEqual(result.output.count("FIXTURE LOKAL"), 2)
        self.assertEqual(sleep.call_count, 4)

    def test_blocked_page_never_looks_like_an_empty_queue(self):
        result, render, sleep = self.invoke_monitor([FixtureError("CAPTCHA terdeteksi.")], once=False)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("CAPTCHA terdeteksi", result.output)
        self.assertNotIn("Tersedia", result.output)
        render.assert_called_once()
        sleep.assert_not_called()


@unittest.skipIf(sync_playwright is None, "Playwright opsional belum terpasang")
class FixtureBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        try:
            cls.page = stack.enter_context(open_fixture_page())
        except FixtureError as exc:
            if "playwright install chromium" not in str(exc):
                raise
            raise unittest.SkipTest(str(exc)) from None

    def test_fixture_states_and_task_rows_in_chromium(self):
        cases = (
            ('<ul data-queue-state="ready"></ul>', []),
            ('<ul data-queue-state="loading"></ul>', None),
            (
                '<ul data-queue-state="ready">'
                '<li data-task-id="1" data-task-kind="follow"></li>'
                '<li data-task-id="2" data-task-kind="like"></li>'
                '<li data-task-id="1" data-task-kind="follow"></li></ul>',
                [Task("1", TaskKind.FOLLOW), Task("2", TaskKind.LIKE)],
            ),
            ('<ul data-queue-state="error"></ul>', "Halaman melaporkan kegagalan"),
            ('<ul data-queue-state="access-denied"></ul>', "Akses ditolak"),
            ('<ul data-queue-state="captcha"></ul>', "CAPTCHA terdeteksi"),
            ('<ul data-queue-state="login-required"></ul>', "Login diperlukan"),
            ("<ul></ul>", "tepat satu elemen"),
            (
                '<ul data-queue-state="ready"><li data-task-kind="like"></li></ul>',
                "tidak memiliki ID",
            ),
            (
                '<ul data-queue-state="ready"></ul>'
                '<script>document.querySelector("ul").dataset.queueState = "error";</script>',
                [],
            ),
        )
        for markup, expected in cases:
            with self.subTest(markup=markup):
                content = markup.encode("utf-8")
                if isinstance(expected, str):
                    with self.assertRaisesRegex(FixtureError, expected):
                        render_fixture(self.page, content)
                else:
                    self.assertEqual(render_fixture(self.page, content), expected)
