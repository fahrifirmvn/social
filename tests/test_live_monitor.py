import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from cabinet_snapshot import parse_cabinet_snapshot
from live_monitor import TASKS_URL, classify_response, decode_response, monitor_live
from main import app
from runner import History, RetryPolicy

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pr-social-snapshot.json"


class LiveMonitorTests(unittest.TestCase):
    def test_classifies_only_observed_first_party_responses(self):
        path = "/api/cabinet/tasks/list"
        self.assertEqual(classify_response(f"https://pr.social{path}?p=1", "GET"), "listing")
        self.assertIsNone(classify_response(f"https://other.example{path}", "GET"))
        self.assertIsNone(classify_response(f"https://pr.social.attacker.example{path}", "GET"))
        self.assertIsNone(classify_response("https://pr.social/api/tasks/101/start", "POST"))
        self.assertEqual(classify_response("https://pr.social/api/tasks/101/api-check", "POST"), "check")

    def test_access_limit_stops_without_reading_body(self):
        for status in (401, 403, 429):
            response = Mock(status=status)
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, f"HTTP {status}"):
                decode_response(response, "listing")
            response.json.assert_not_called()

    def test_ignores_other_platform_and_shows_queued_without_finalizing(self):
        payload = json.loads(FIXTURE.read_bytes())
        payload["listing"]["list"]["net"] = "other"
        self.assertIsNone(decode_response(Mock(status=200, json=Mock(return_value=payload["listing"])), "listing"))
        self.assertIn("Tertunda", decode_response(Mock(status=200, json=Mock(return_value=payload["check"])), "check"))

    def test_cli_requires_authorization_confirmation(self):
        with patch("main.monitor_live") as launch:
            result = CliRunner().invoke(app, ["monitor", "--live"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--confirm-authorized", result.output)
        launch.assert_not_called()

    def test_cli_live_listing_is_displayed_without_writing_files(self):
        state = parse_cabinet_snapshot(FIXTURE.read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = (History(root / "history.json"), root / "activity.log", 1, RetryPolicy())
            with (
                patch("main.load_settings", return_value=settings),
                patch("main.monitor_live", side_effect=lambda render, _check, *, once: render(state)) as launch,
            ):
                result = CliRunner().invoke(app, ["monitor", "--live", "--confirm-authorized", "--once"])
            self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("LIVE BACA-SAJA", result.output)
        self.assertTrue(launch.call_args.kwargs["once"])

    def test_browser_loop_only_opens_task_page_and_reads_known_instagram_checks(self):
        try:
            import playwright.sync_api
        except ImportError:
            self.skipTest("Playwright opsional belum terpasang")

        payload = json.loads(FIXTURE.read_bytes())
        page = Mock()
        page.url = "https://pr.social/tasks"
        page.is_closed.return_value = True

        def response(url, method, data):
            request = Mock(method=method, is_navigation_request=Mock(return_value=False))
            return Mock(url=url, request=request, status=200, json=Mock(return_value=data))

        listing = response("https://pr.social/api/cabinet/tasks/list", "GET", payload["listing"])
        matching = response("https://pr.social/api/tasks/101/api-check", "POST", payload["check"])
        unknown = response("https://pr.social/api/tasks/999/api-check", "POST", payload["check"])

        def open_page(*_args, **_kwargs):
            callback = page.on.call_args.args[1]
            for item in (listing, unknown, matching):
                callback(item)

        page.goto.side_effect = open_page

        # Arsitektur sekarang: connect ke browser lewat CDP, bukan launch baru.
        context = Mock()
        context.pages = [page]
        browser = Mock()
        browser.contexts = [context]

        on_listing, on_check = Mock(), Mock()
        with (
            patch("live_monitor._ensure_browser_running", return_value=None),
            patch("live_monitor.cleanup_browser"),
            patch("live_monitor._wait_for_turnstile"),
            patch("live_monitor._connect_cdp", return_value=browser),
            patch("playwright.sync_api.sync_playwright"),
        ):
            monitor_live(on_listing, on_check, once=False)

        page.goto.assert_called_once_with(TASKS_URL, wait_until="domcontentloaded")
        page.reload.assert_not_called()
        page.click.assert_not_called()
        # Browser CDP milik pengguna — jangan ditutup oleh monitor.
        browser.close.assert_not_called()
        on_listing.assert_called_once()
        on_check.assert_called_once()
        unknown.json.assert_not_called()
