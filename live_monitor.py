"""Monitor Playwright pasif untuk navigasi PR.SOCIAL yang dilakukan pengguna."""

import logging
import os
import re
import shutil
import signal
import subprocess
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from cabinet_snapshot import CabinetSnapshot, parse_cabinet_listing, parse_verification_status
from client import INSTAGRAM

if TYPE_CHECKING:
    from playwright.sync_api import Response

logger = logging.getLogger("pr_social")
TASKS_URL = "https://pr.social/tasks"
CDP_ENDPOINT = "http://localhost:9222"
CDP_PORT = 9222

# Persistent profile — login & cookies tersimpan antar sesi
_PROFILE_DIR = Path(__file__).resolve().parent / "data" / "chrome-profile"

# Virtual display untuk background mode
_VIRTUAL_DISPLAY = ":99"

# Urutan pencarian browser di system
_BROWSER_CANDIDATES = [
    ["flatpak", "run", "org.chromium.Chromium"],
    ["google-chrome-stable"],
    ["google-chrome"],
    ["chromium-browser"],
    ["chromium"],
    ["snap", "run", "chromium"],
]


def classify_response(url: str, method: str, *, navigation: bool = False) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "pr.social":
        return None
    if navigation:
        return "document"
    if method == "GET" and parsed.path == "/api/cabinet/tasks/list":
        return "listing"
    if method == "POST" and re.fullmatch(r"/api/tasks/[0-9]+/api-check", parsed.path):
        return "check"
    return None


def decode_response(response: "Response", kind: str) -> CabinetSnapshot | str | None:
    if response.status in (401, 403, 429):
        raise ValueError(f"Pemantauan dihentikan: PR.SOCIAL mengembalikan HTTP {response.status}.")
    if kind == "document":
        return None
    if not 200 <= response.status < 300:
        raise ValueError(f"Respons task tidak bisa dibaca: HTTP {response.status}.")

    try:
        payload = response.json()
    except ValueError:
        raise ValueError("Respons task bukan JSON yang bisa dibaca.") from None
    if kind == "check":
        return parse_verification_status(payload)
    if not isinstance(payload, dict) or not isinstance(payload.get("list"), dict):
        raise ValueError("Respons daftar task tidak memiliki struktur yang dikenali.")
    if payload["list"].get("net") != INSTAGRAM:
        return None
    return parse_cabinet_listing(payload)


# ── browser management ───────────────────────────────────────────

def _cdp_is_ready() -> bool:
    try:
        resp = httpx.get(f"{CDP_ENDPOINT}/json/version", timeout=2)
        return resp.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


def _find_browser_command() -> list[str] | None:
    for candidate in _BROWSER_CANDIDATES:
        if shutil.which(candidate[0]) is not None:
            return candidate
    return None


def _start_xvfb() -> subprocess.Popen | None:
    """Start Xvfb virtual display. Return Popen handle atau None."""
    if not shutil.which("Xvfb"):
        return None

    # Cek apakah display sudah jalan
    try:
        result = subprocess.run(
            ["xdpyinfo", "-display", _VIRTUAL_DISPLAY],
            capture_output=True, timeout=3,
        )
        if result.returncode == 0:
            logger.info("Virtual display %s sudah aktif.", _VIRTUAL_DISPLAY)
            return None  # Sudah jalan, nggak perlu start
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    logger.info("Memulai Xvfb di display %s…", _VIRTUAL_DISPLAY)
    proc = subprocess.Popen(
        ["Xvfb", _VIRTUAL_DISPLAY, "-screen", "0", "1920x1080x24", "-ac"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    if proc.poll() is not None:
        logger.warning("Xvfb gagal start.")
        return None
    logger.info("Xvfb aktif di display %s.", _VIRTUAL_DISPLAY)
    return proc


def _ensure_browser_running(*, background: bool = False) -> subprocess.Popen | None:
    """Launch browser dengan CDP kalau belum jalan.

    Args:
        background: Jalankan di virtual display (Xvfb) supaya nggak ganggu layar.

    Return Popen handle kalau kita yang launch, None kalau sudah jalan.
    """
    if _cdp_is_ready():
        logger.info("Browser sudah jalan di port %d.", CDP_PORT)
        return None

    cmd = _find_browser_command()
    if cmd is None:
        raise ValueError(
            "Tidak ditemukan Chrome/Chromium. "
            "Install via: pacman -S chromium / flatpak install org.chromium.Chromium"
        )

    # Persistent profile
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    launch_cmd = [
        *cmd,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    env = os.environ.copy()
    xvfb_proc = None

    if background:
        xvfb_proc = _start_xvfb()
        env["DISPLAY"] = _VIRTUAL_DISPLAY
        logger.info("Browser akan jalan di background (display %s).", _VIRTUAL_DISPLAY)

    logger.info("Meluncurkan browser: %s", " ".join(launch_cmd))
    proc = subprocess.Popen(
        launch_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    # Tunggu CDP siap
    for _ in range(30):
        if proc.poll() is not None:
            if xvfb_proc:
                xvfb_proc.terminate()
            raise ValueError("Browser gagal diluncurkan.")
        if _cdp_is_ready():
            logger.info("Browser siap di port %d.", CDP_PORT)
            # Simpan xvfb_proc di atribut supaya bisa di-cleanup
            proc._xvfb = xvfb_proc  # type: ignore[attr-defined]
            return proc
        time.sleep(0.5)

    proc.terminate()
    if xvfb_proc:
        xvfb_proc.terminate()
    raise ValueError(f"Browser tidak siap dalam 15 detik di port {CDP_PORT}.")


def cleanup_browser(proc: subprocess.Popen | None) -> None:
    """Bersihkan browser dan Xvfb yang kita launch."""
    if proc is None:
        return
    if proc.poll() is None:
        logger.info("Menutup browser.")
        proc.terminate()
        proc.wait(timeout=5)
    xvfb = getattr(proc, "_xvfb", None)
    if xvfb and xvfb.poll() is None:
        logger.info("Menutup Xvfb.")
        xvfb.terminate()
        xvfb.wait(timeout=3)


def _connect_cdp(playwright):
    browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT, timeout=5000)
    logger.info("Terhubung ke browser via CDP di %s", CDP_ENDPOINT)
    return browser


def _wait_for_turnstile(page, *, timeout: float = 120) -> None:
    if "/login" not in page.url and "challenge" not in page.url:
        return

    logger.info(
        "Menunggu login & Turnstile… (timeout %ds)",
        int(timeout),
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = page.url
        if "/login" not in url and "challenge" not in url:
            logger.info("Login berhasil.")
            return
        page.wait_for_timeout(1000)

    raise ValueError("Timeout menunggu login/Turnstile.")


# ── live monitor ─────────────────────────────────────────────────

def monitor_live(
    on_listing: Callable[[CabinetSnapshot], None],
    on_check: Callable[[str], None],
    *,
    once: bool,
) -> None:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise ValueError("Pasang Playwright: pip install -r requirements-playwright.txt") from None

    browser_proc = _ensure_browser_running()

    try:
        with sync_playwright() as playwright:
            browser = _connect_cdp(playwright)

            try:
                contexts = browser.contexts
                if contexts and contexts[0].pages:
                    page = contexts[0].pages[0]
                    logger.info("Menggunakan tab: %s", page.url)
                else:
                    page = (contexts[0] if contexts else browser.new_context()).new_page()

                if "pr.social" not in page.url:
                    page.goto(TASKS_URL, wait_until="domcontentloaded")

                _wait_for_turnstile(page)

                pending: deque[tuple[Response, str]] = deque()
                instagram_ids: set[int] = set()

                def observe(response: "Response") -> None:
                    request = response.request
                    navigation = request.is_navigation_request() and request.frame == page.main_frame
                    kind = classify_response(response.url, request.method, navigation=navigation)
                    if kind is not None:
                        pending.append((response, kind))

                page.on("response", observe)

                if "pr.social" in page.url and "/login" not in page.url:
                    page.goto(TASKS_URL, wait_until="domcontentloaded")

                while pending or not page.is_closed():
                    while pending:
                        response, kind = pending.popleft()
                        if kind == "check":
                            task_id = int(urlsplit(response.url).path.split("/")[-2])
                            if task_id not in instagram_ids:
                                continue
                        result = decode_response(response, kind)
                        if kind == "listing":
                            instagram_ids = {row.id for row in result.rows} if isinstance(result, CabinetSnapshot) else set()
                        if isinstance(result, CabinetSnapshot):
                            on_listing(result)
                            if once:
                                return
                        elif isinstance(result, str):
                            on_check(result)
                    if not page.is_closed():
                        page.wait_for_timeout(250)
            finally:
                pass
    except Error as exc:
        if "Executable doesn't exist" in str(exc):
            raise ValueError("Pasang Chromium: python -m playwright install chromium") from None
        raise ValueError(f"Browser live gagal: {exc}") from None
    finally:
        cleanup_browser(browser_proc)
