"""Workflow satu task Follow pada fixture lokal; tidak ada akses live."""

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from fixture_monitor import open_fixture_page
from runner import History, write_json_atomically

if TYPE_CHECKING:
    from playwright.sync_api import Page

DEMO_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "follow-workflow.html"
FINAL_PHASES = {"verified", "failed"}
PHASES = FINAL_PHASES | {"starting", "following", "checking", "queued"}


@dataclass(frozen=True)
class FollowTask:
    id: str
    target: str
    quota: int


@dataclass(frozen=True)
class Checkpoint:
    task_id: str
    phase: str


class FollowView(Protocol):
    def task(self) -> FollowTask: ...
    def start(self) -> None: ...
    def profile(self) -> tuple[str, bool]: ...
    def follow(self) -> None: ...
    def submit_delay(self) -> int: ...
    def check(self) -> None: ...
    def status(self) -> str: ...


def read_checkpoint(path: Path) -> Checkpoint | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (
        not isinstance(data, dict)
        or data.get("source") != "follow-demo"
        or not isinstance(data.get("task_id"), str)
        or not isinstance(data.get("phase"), str)
        or data["phase"] not in PHASES
    ):
        raise ValueError("Checkpoint Follow demo tidak dikenali; tidak ada aksi yang diulang.")
    return Checkpoint(data["task_id"], data["phase"])


def work_one(view: FollowView, checkpoint_path: Path) -> Checkpoint | None:
    """Jalankan maksimal satu task. Simpan checkpoint sebelum setiap aksi."""
    if (existing := read_checkpoint(checkpoint_path)) is not None:
        return existing

    task = view.task()
    if task.quota == 0:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", task.target):
        raise ValueError("Target Follow tidak dikenali.")

    def save(phase: str) -> Checkpoint:
        checkpoint = Checkpoint(task.id, phase)
        write_json_atomically(checkpoint_path, {**asdict(checkpoint), "source": "follow-demo"})
        return checkpoint

    # Cek ulang identitas dan kuota sebelum mulai, bukan setelah aksi.
    current = view.task()
    if current.quota == 0:
        return None
    if (current.id, current.target) != (task.id, task.target):
        raise ValueError("Task berubah sebelum Start; tidak ada aksi yang dilakukan.")
    save("starting")
    view.start()
    started_at = time.monotonic()
    target, following = view.profile()
    if target != task.target:
        raise ValueError("Target tidak cocok dengan task; Follow tidak diklik.")
    if not following:
        save("following")
        view.follow()
    if view.profile() != (task.target, True):
        raise ValueError("Status Follow tidak dapat dikonfirmasi; verifikasi tidak diajukan.")

    remaining = started_at + view.submit_delay() - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    if view.profile() != (task.target, True):
        raise ValueError("Target atau status Follow berubah; verifikasi tidak diajukan.")
    save("checking")
    view.check()
    result = view.status()
    if result not in FINAL_PHASES | {"queued"}:
        raise ValueError("Hasil verifikasi tidak dikenali; task tidak dianggap selesai.")
    return save(result)


class FixtureFollowView:
    """Locator ini kontrak fixture, bukan locator PR.SOCIAL atau Instagram."""

    def __init__(self, page: "Page") -> None:
        self.root = page.locator("#follow-demo")

    def _attr(self, name: str) -> str:
        value = self.root.get_attribute(f"data-{name}")
        if not value:
            raise ValueError(f"Fixture Follow tidak memiliki atribut {name}.")
        return value

    def _natural(self, name: str) -> int:
        value = self._attr(name)
        if not re.fullmatch(r"[0-9]{1,6}", value):
            raise ValueError(f"Angka {name} pada fixture Follow tidak valid.")
        return int(value)

    def task(self) -> FollowTask:
        return FollowTask(self._attr("task-id"), self._attr("target"), self._natural("quota"))

    def start(self) -> None:
        self.root.get_by_role("button", name="Start", exact=True).click()

    def profile(self) -> tuple[str, bool]:
        state = self._attr("following")
        if state not in {"true", "false"}:
            raise ValueError("Status Follow pada fixture tidak dikenali.")
        return self._attr("profile"), state == "true"

    def follow(self) -> None:
        self.root.get_by_role("button", name="Follow", exact=True).click()

    def submit_delay(self) -> int:
        return self._natural("submit-delay")

    def check(self) -> None:
        self.root.get_by_role("button", name="Cek", exact=True).click()

    def status(self) -> str:
        return self._attr("status")


def run_follow_demo(path: Path) -> tuple[Checkpoint | None, bool]:
    journal = History(path)
    with journal.write_lock():
        if (existing := read_checkpoint(journal.path)) is not None:
            return existing, True
        with open_fixture_page(enable_javascript=True) as page:
            page.set_content(DEMO_FIXTURE.read_text(encoding="utf-8"), wait_until="domcontentloaded")
            return work_one(FixtureFollowView(page), journal.path), False
