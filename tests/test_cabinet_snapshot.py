import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from cabinet_snapshot import describe_changes, parse_cabinet_snapshot
from main import app
from runner import History, RetryPolicy

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pr-social-snapshot.json"


class CabinetSnapshotTests(unittest.TestCase):
    def test_paged_counts_and_queued_verification(self):
        state = parse_cabinet_snapshot(FIXTURE.read_bytes())
        self.assertEqual((state.total, state.follow, state.like), (12, 11, 1))
        self.assertEqual((state.page, state.pages, len(state.rows), state.per_page), (1, 2, 10, 10))
        self.assertEqual(state.rows[0].left, 8)
        self.assertIn("Tertunda", state.verification)
        self.assertIn("25 menit", state.verification)

    def test_unknown_result_is_not_treated_as_success(self):
        payload = json.loads(FIXTURE.read_bytes())
        payload["check"].update(code="other", found=True)
        state = parse_cabinet_snapshot(json.dumps(payload).encode())
        self.assertIn("tidak dianggap sebagai hasil final", state.verification)

    def test_failed_listing_is_not_treated_as_empty(self):
        payload = json.loads(FIXTURE.read_bytes())
        payload["listing"]["ok"] = False
        with self.assertRaisesRegex(ValueError, "belum siap atau gagal"):
            parse_cabinet_snapshot(json.dumps(payload).encode())

    def test_partial_page_uses_reported_total_not_sum_of_quota(self):
        payload = json.loads(FIXTURE.read_bytes())
        page = payload["listing"]["list"]
        page["page"] = 2
        page["rows"] = [{"id": 120, "platform": "instagram", "action": "like", "left": 50}]
        state = parse_cabinet_snapshot(json.dumps(payload).encode())
        self.assertEqual((state.total, len(state.rows), state.per_page), (12, 1, 10))

    def test_other_pages_and_filters_do_not_report_tasks_disappearing(self):
        state = parse_cabinet_snapshot(FIXTURE.read_bytes())
        self.assertEqual(describe_changes(state, replace(state, page=2, rows=())), [])
        self.assertEqual(
            describe_changes(state, replace(state, filters=("instagram", "like", None), rows=())),
            [],
        )

    def test_requires_instagram_scope_and_rows(self):
        payload = json.loads(FIXTURE.read_bytes())
        page = payload["listing"]["list"]
        page["net"] = "all"
        with self.assertRaisesRegex(ValueError, "difilter ke Instagram"):
            parse_cabinet_snapshot(json.dumps(payload).encode())
        page["net"] = "instagram"
        page["rows"][0]["platform"] = "tiktok"
        with self.assertRaisesRegex(ValueError, "platform lain"):
            parse_cabinet_snapshot(json.dumps(payload).encode())

    def test_cli_polling_reports_quota_and_page_membership_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "snapshot.json"
            path.write_bytes(FIXTURE.read_bytes())

            def next_poll(_interval):
                if sleep.call_count == 2:
                    raise KeyboardInterrupt
                payload = json.loads(path.read_bytes())
                page = payload["listing"]["list"]
                page["total"] = 11
                page["rows"][0]["left"] = 0
                page["rows"][1]["left"] = 14
                page["rows"].pop()
                page["rows"].append({"id": 111, "platform": "instagram", "action": "follow", "left": 5})
                next_path = root / "next.json"
                next_path.write_text(json.dumps(payload), encoding="utf-8")
                next_path.replace(path)

            settings = (History(root / "history.json"), root / "activity.log", 1, RetryPolicy())
            with (
                patch("main.load_settings", return_value=settings),
                patch("main.time.sleep", side_effect=next_poll) as sleep,
            ):
                result = CliRunner().invoke(app, ["monitor", "--snapshot", str(path)])
        self.assertEqual(result.exit_code, 130, result.output)
        self.assertIn("Total daftar: 12 → 11", result.output)
        self.assertIn("#101 sisa kuota: 8 → 0", result.output)
        self.assertIn("#102 sisa kuota: 12 → 14", result.output)
        self.assertIn("#111 muncul di halaman", result.output)
        self.assertIn("#110 tidak lagi terlihat di halaman ini", result.output)
        self.assertIn("0 (habis)", result.output)

    def test_cli_snapshot_only_reads_local_data(self):
        with tempfile.TemporaryDirectory() as directory:
            history = History(Path(directory) / "data" / "history.json")
            log = Path(directory) / "logs" / "activity.log"
            with patch("main.load_settings", return_value=(history, log, 1, RetryPolicy())):
                result = CliRunner().invoke(app, ["monitor", "--snapshot", str(FIXTURE), "--once"])
            self.assertFalse(history.path.parent.exists())
            self.assertFalse(log.parent.exists())
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("SNAPSHOT LOKAL", result.output)
        self.assertIn("10/10", result.output)
        self.assertIn("Tertunda (queued)", result.output)
