"""Baca antrean dari HTML lokal dengan Playwright standar, tanpa akses live."""

from contextlib import contextmanager
from typing import TYPE_CHECKING

from client import Task, TaskKind

if TYPE_CHECKING:
    from playwright.sync_api import Page


class FixtureError(ValueError):
    """Antrean tidak dapat dibaca dengan aman."""


@contextmanager
def open_fixture_page(*, enable_javascript: bool = False):
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise FixtureError(
            "Pasang Playwright dengan: python -m pip install -r requirements-playwright.txt"
        ) from None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    java_script_enabled=enable_javascript, offline=True, service_workers="block",
                )
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                page.set_default_timeout(5_000)
                yield page
            finally:
                browser.close()
    except Error as exc:
        if "Executable doesn't exist" in str(exc):
            raise FixtureError("Pasang Chromium dengan: python -m playwright install chromium") from None
        raise FixtureError("Playwright gagal membaca fixture atau menjalankan Chromium.") from None


def render_fixture(page: "Page", content: bytes) -> list[Task] | None:
    """Kembalikan None saat loading, atau daftar task setelah antrean siap."""
    page.set_content(content.decode("utf-8"), wait_until="domcontentloaded")
    queue = page.locator("[data-queue-state]")
    if queue.count() != 1:
        raise FixtureError("Fixture harus memiliki tepat satu elemen data-queue-state.")

    state = queue.get_attribute("data-queue-state")
    if state == "loading":
        return None
    blocked = {
        "login-required": "Login diperlukan.",
        "captcha": "CAPTCHA terdeteksi.",
        "access-denied": "Akses ditolak.",
        "error": "Halaman melaporkan kegagalan.",
    }
    if state in blocked:
        raise FixtureError(f"Pemantauan dihentikan: {blocked[state]}")
    if state != "ready":
        raise FixtureError("Status antrean fixture tidak dikenali.")

    tasks: dict[str, Task] = {}
    for row in queue.locator("[data-task-id], [data-task-kind]").all():
        task_id = (row.get_attribute("data-task-id") or "").strip()
        raw_kind = (row.get_attribute("data-task-kind") or "").strip().lower()
        if not task_id:
            raise FixtureError("Baris fixture tidak memiliki ID task.")
        try:
            kind = TaskKind(raw_kind)
        except ValueError:
            raise FixtureError("Jenis task fixture harus follow atau like.") from None
        task = Task(task_id, kind)
        if task_id in tasks and tasks[task_id] != task:
            raise FixtureError("Fixture berisi ID task yang sama dengan jenis berbeda.")
        tasks.setdefault(task_id, task)
    return list(tasks.values())
