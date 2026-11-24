"""Eksekusi task PR.SOCIAL secara otomatis via browser CDP."""

import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger("pr_social")

BASE = Path(__file__).resolve().parent
TASKS_URL = "https://pr.social/tasks"

# ── Lokasi file data ────────────────────────────────────────────
RESULTS_DIR = BASE / "data"
COUNTER_FILE = RESULTS_DIR / "action_counter.json"
CONFIG_PATH = BASE / "config.json"


# ── Konfigurasi executor (dari config.json "live") ──────────────

@dataclass
class ExecutorConfig:
    daily_follow_limit: int = 150
    daily_like_limit: int = 150
    handle_like_tasks: bool = True
    max_step_retries: int = 2
    max_consecutive_fails: int = 5
    # Gaussian delay: (mean, stddev, floor, ceiling) dalam detik
    delay_between_tasks: tuple = (4.0, 1.5, 2.0, 8.0)
    delay_after_action: tuple = (2.0, 0.8, 1.0, 4.0)
    delay_ui_settle: float = 1.5
    timeout_element: int = 8_000
    timeout_navigation: int = 12_000
    timeout_fast: int = 2_000
    error_cooldown_base: float = 10.0
    error_cooldown_factor: float = 2.0
    error_cooldown_max: float = 120.0

    def limit_for(self, action: str) -> int:
        return self.daily_like_limit if action == "like" else self.daily_follow_limit


def _as_int(value, default: int, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return default
    return value


def _as_float(value, default: float, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        return default
    return float(value)


def _as_bool(value, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_delay(value, default: tuple) -> tuple:
    if (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)
        and value[2] <= value[3]  # floor <= ceiling
    ):
        return tuple(float(x) for x in value)
    return default


def load_executor_config(config_path: Path | None = None) -> ExecutorConfig:
    """Muat section 'live' dari config.json. Field invalid jatuh ke default."""
    cfg = ExecutorConfig()
    path = config_path or CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        live = data.get("live", {}) if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        logger.info("config.json tidak terbaca; pakai default executor.")
        return cfg
    if not isinstance(live, dict):
        return cfg

    return ExecutorConfig(
        daily_follow_limit=_as_int(live.get("daily_follow_limit"), cfg.daily_follow_limit),
        daily_like_limit=_as_int(live.get("daily_like_limit"), cfg.daily_like_limit),
        handle_like_tasks=_as_bool(live.get("handle_like_tasks"), cfg.handle_like_tasks),
        max_step_retries=_as_int(live.get("max_step_retries"), cfg.max_step_retries),
        max_consecutive_fails=_as_int(live.get("max_consecutive_fails"), cfg.max_consecutive_fails),
        delay_between_tasks=_as_delay(live.get("delay_between_tasks"), cfg.delay_between_tasks),
        delay_after_action=_as_delay(live.get("delay_after_action"), cfg.delay_after_action),
        delay_ui_settle=_as_float(live.get("delay_ui_settle"), cfg.delay_ui_settle, minimum=0.1),
        timeout_element=_as_int(live.get("timeout_element"), cfg.timeout_element, minimum=500),
        timeout_navigation=_as_int(live.get("timeout_navigation"), cfg.timeout_navigation, minimum=500),
        timeout_fast=_as_int(live.get("timeout_fast"), cfg.timeout_fast, minimum=200),
        error_cooldown_base=_as_float(live.get("error_cooldown_base"), cfg.error_cooldown_base),
        error_cooldown_factor=_as_float(live.get("error_cooldown_factor"), cfg.error_cooldown_factor, minimum=1.0),
        error_cooldown_max=_as_float(live.get("error_cooldown_max"), cfg.error_cooldown_max),
    )


# ── Counter aksi harian (follow & like terpisah) ────────────────

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_counter() -> dict:
    try:
        data = json.loads(COUNTER_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("date") == _today_str():
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"date": _today_str(), "follow": 0, "like": 0}


def get_daily_count(action: str) -> int:
    data = _read_counter()
    value = data.get(action, 0)
    return value if isinstance(value, int) else 0


def increment_daily_count(action: str) -> int:
    COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = _read_counter()
    data[action] = get_daily_count(action) + 1
    COUNTER_FILE.write_text(json.dumps(data) + "\n", encoding="utf-8")
    return data[action]


# Kompatibilitas lama
def get_daily_follows() -> int:
    return get_daily_count("follow")


# ── data classes ─────────────────────────────────────────────────

@dataclass
class TaskResult:
    task_index: int
    username: str
    status: str       # submitted | skipped | skipped_slot | skipped_like | failed | limit_reached
    message: str
    kind: str = "follow"
    earning: float = 0.0
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SessionStats:
    started_at: str = ""
    ended_at: str = ""
    interrupted: bool = False
    total_tasks: int = 0
    submitted: int = 0
    failed: int = 0
    skipped: int = 0
    skipped_slot: int = 0
    skipped_like: int = 0
    total_earning: float = 0.0
    results: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


class PageState(Enum):
    READY_TO_START = "ready_to_start"
    STARTED        = "started"
    READY_TO_CHECK = "ready_to_check"
    REVIEWING      = "reviewing"
    SLOT_FULL      = "slot_full"
    TASK_LIST      = "task_list"


class TaskExecutor:

    def __init__(self, page: "Page", context: "BrowserContext",
                 config: ExecutorConfig | None = None) -> None:
        self.page = page
        self.context = context
        self.cfg = config or ExecutorConfig()
        self.results: list[TaskResult] = []
        self.stats = SessionStats()
        self._current_kind = "follow"

    # ── timing helpers ───────────────────────────────────────────

    @staticmethod
    def _human_delay(params: tuple) -> float:
        mean, std, lo, hi = params
        return max(lo, min(hi, random.gauss(mean, std)))

    def _sleep(self, params: tuple) -> None:
        d = self._human_delay(params)
        logger.info("Jeda %.1f dtk…", d)
        time.sleep(d)

    def _error_cooldown(self, fails: int) -> None:
        if fails <= 0 or self.cfg.error_cooldown_base <= 0:
            return
        delay = min(
            self.cfg.error_cooldown_base * (self.cfg.error_cooldown_factor ** (fails - 1)),
            self.cfg.error_cooldown_max,
        )
        logger.info("Error cooldown: %.0f dtk (fail ke-%d)…", delay, fails)
        time.sleep(delay)

    def _settle(self) -> None:
        self.page.wait_for_timeout(int(self.cfg.delay_ui_settle * 1000))

    def _js_click(self, locator) -> bool:
        try:
            locator.evaluate("el => el.click()")
            return True
        except Exception:
            return False

    def _is_visible(self, locator) -> bool:
        try:
            return locator.is_visible(timeout=self.cfg.timeout_fast)
        except Exception:
            return False

    @staticmethod
    def _is_visible_on(page: "Page", selector: str, timeout: int) -> bool:
        try:
            return page.locator(selector).first.is_visible(timeout=timeout)
        except Exception:
            return False

    def _retry(self, fn, label: str):
        for attempt in range(1, self.cfg.max_step_retries + 1):
            result = fn()
            if result is not None and result is not False:
                return result
            if attempt < self.cfg.max_step_retries:
                logger.info("Retry %s (%d/%d)…", label, attempt, self.cfg.max_step_retries)
                self._settle()
        logger.warning("%s gagal setelah %d percobaan.", label, self.cfg.max_step_retries)
        return None

    # ── state & kind detection ───────────────────────────────────

    def detect_state(self) -> PageState:
        if self._is_visible(self.page.locator("text=/slot sudah/i")):
            logger.info("State: SLOT_FULL")
            return PageState.SLOT_FULL
        if self._is_visible(self.page.locator("text=/[Tt]ugas berikutnya/")):
            logger.info("State: REVIEWING")
            return PageState.REVIEWING
        if self._is_visible(self.page.locator("text=/Periksa/i")):
            logger.info("State: READY_TO_CHECK")
            return PageState.READY_TO_CHECK
        ig_visible = (
            self._is_visible(self.page.locator("a[href*='instagram.com']"))
            or self._is_visible(self.page.locator("text=/instagram\\.com\\//i"))
            or self._is_visible(self.page.locator("text=/www\\.instagram/i"))
        )
        if ig_visible:
            logger.info("State: STARTED")
            return PageState.STARTED
        if self._is_visible(self.page.locator("text=/Mulai.*\\$/")):
            logger.info("State: READY_TO_START")
            return PageState.READY_TO_START
        logger.info("State: TASK_LIST")
        return PageState.TASK_LIST

    def _detect_task_kind(self) -> str:
        """Tentukan jenis task yang sedang terbuka di panel detail."""
        if self._is_visible(self.page.locator("text=/[Ss]uka[i]? di Instagram/")):
            return "like"
        return "follow"

    def _check_session_alive(self) -> bool:
        if "/login" in self.page.url or "challenge" in self.page.url:
            logger.warning("Sesi expired — halaman login terdeteksi.")
            from live_monitor import _wait_for_turnstile
            _wait_for_turnstile(self.page, timeout=180)
            if "/login" in self.page.url:
                return False
            self.navigate_to_tasks()
        return True

    # ── navigasi ─────────────────────────────────────────────────

    def navigate_to_tasks(self) -> None:
        logger.info("Navigasi ke daftar tugas…")
        self.page.goto(
            f"{TASKS_URL}?net=instagram",
            wait_until="domcontentloaded",
            timeout=self.cfg.timeout_navigation,
        )
        try:
            self.page.locator("text=/\\$0\\.\\d/").first.wait_for(
                state="visible", timeout=self.cfg.timeout_element
            )
        except Exception:
            self._settle()

    # ── pilih task ───────────────────────────────────────────────

    def select_task(self, index: int = 0) -> str | None:
        rows = self.page.locator("text=/(?:[Ii]kuti|[Ss]uka[i]?) di Instagram/").all()
        logger.info("Task rows: %d", len(rows))
        if not rows:
            return None

        target = index
        chosen_text = ""
        for i, row in enumerate(rows):
            try:
                if not row.is_visible(timeout=self.cfg.timeout_fast):
                    continue
                bbox = row.bounding_box()
                if not bbox:
                    continue
                if target > 0:
                    target -= 1
                    continue
                chosen_text = row.text_content(timeout=self.cfg.timeout_fast) or ""
                self.page.mouse.click(
                    bbox["x"] + bbox["width"] / 2,
                    bbox["y"] + bbox["height"] / 2,
                )
                logger.info("Klik task row #%d.", index)
                break
            except Exception:
                continue
        else:
            return None

        self._current_kind = "like" if re.search(r"[Ss]uka", chosen_text) else "follow"
        self._settle()
        return self._read_target_username() or "unknown"

    def _read_target_username(self) -> str | None:
        for el in self.page.locator("text=/^@[A-Za-z0-9._]/").all():
            try:
                t = el.text_content(timeout=self.cfg.timeout_fast)
                if t and t.startswith("@") and len(t) > 2:
                    return t.strip()
            except Exception:
                continue
        return None

    def _read_earning(self) -> float:
        for el in self.page.locator("text=/\\$0\\.\\d{2,4}/").all():
            try:
                t = el.text_content(timeout=self.cfg.timeout_fast)
                if t:
                    return float(t.strip().replace("$", "").replace(",", "."))
            except (ValueError, Exception):
                continue
        return 0.0

    # ── mulai task ───────────────────────────────────────────────

    def _try_start_task(self) -> str | None:
        btn_loc = self.page.locator("text=/Mulai.*\\$/")
        try:
            btn_loc.first.wait_for(state="visible", timeout=self.cfg.timeout_element)
        except Exception:
            return None

        clicked = False
        for el in btn_loc.all():
            if self._js_click(el):
                clicked = True
                break
        if not clicked:
            for el in reversed(self.page.locator("text=/Mulai/i").all()):
                try:
                    if el.is_visible(timeout=self.cfg.timeout_fast) and self._js_click(el):
                        clicked = True
                        break
                except Exception:
                    continue
        if not clicked:
            return None

        try:
            self.page.locator(
                "a[href*='instagram.com'], text=/instagram\\.com\\//i, text=/slot sudah/i"
            ).first.wait_for(state="visible", timeout=self.cfg.timeout_element)
        except Exception:
            self._settle()

        if self._is_visible(self.page.locator("text=/slot sudah/i")):
            return "SLOT_FULL"
        return self._get_instagram_url()

    def start_task(self) -> str | None:
        result = self._retry(self._try_start_task, "Klik Mulai")
        if result:
            logger.info("Mulai berhasil." if result != "SLOT_FULL" else "Slot penuh.")
        return result

    def _get_instagram_url(self) -> str | None:
        for link in self.page.locator("a[href*='instagram.com']").all():
            try:
                href = link.get_attribute("href", timeout=self.cfg.timeout_fast)
                if href:
                    logger.info("IG URL: %s", href)
                    return href
            except Exception:
                continue
        for pattern in ("text=/instagram\\.com\\//i", "text=/www\\.instagram/i"):
            for el in self.page.locator(pattern).all():
                try:
                    t = el.text_content(timeout=self.cfg.timeout_fast)
                    if t and "instagram" in t.lower():
                        url = t.strip()
                        if not url.startswith("http"):
                            url = f"https://{url}"
                        logger.info("IG URL (teks): %s", url)
                        return url
                except Exception:
                    continue
        try:
            if self.page.locator("text=/[Tt]autan akan terbuka/").first.is_visible(
                timeout=self.cfg.timeout_fast
            ):
                logger.warning("Mulai belum terklik.")
        except Exception:
            pass
        return None

    # ── aksi Instagram: shared ───────────────────────────────────

    def _dismiss_ig_popups(self, ig: "Page") -> None:
        for pat in (
            "text=/Not Now/i", "text=/Lain Kali/i", "text=/Nanti saja/i",
            "button >> text=/Decline/i", "text=/Tolak/i",
        ):
            try:
                btn = ig.locator(pat).first
                if btn.is_visible(timeout=800):
                    btn.click(timeout=2000)
                    logger.info("Popup IG ditutup.")
                    ig.wait_for_timeout(500)
            except Exception:
                pass

    # ── aksi Instagram: follow ───────────────────────────────────

    def _try_follow(self, ig_url: str) -> bool | None:
        ig = self.context.new_page()
        try:
            ig.goto(ig_url, wait_until="domcontentloaded", timeout=self.cfg.timeout_navigation)
            try:
                ig.locator(
                    "button >> text=/Follow|Ikuti|Mengikuti|Following|Requested|Diminta/i"
                ).first.wait_for(state="visible", timeout=self.cfg.timeout_element)
            except Exception:
                pass

            self._dismiss_ig_popups(ig)

            try:
                if ig.locator(
                    "button >> text=/Following|Mengikuti|Requested|Diminta/i"
                ).first.is_visible(timeout=self.cfg.timeout_fast):
                    logger.info("Sudah following.")
                    return True
            except Exception:
                pass

            follow_btn = ig.locator("button >> text=/^Follow$|^Ikuti$/i").first
            try:
                follow_btn.click(timeout=self.cfg.timeout_element)
                logger.info("Follow diklik.")
            except Exception:
                logger.warning("Tombol Follow tidak ditemukan.")
                return None

            self._sleep(self.cfg.delay_after_action)
            self._dismiss_ig_popups(ig)

            try:
                if ig.locator(
                    "button >> text=/Following|Mengikuti|Requested|Diminta/i"
                ).first.is_visible(timeout=self.cfg.timeout_fast):
                    logger.info("Follow dikonfirmasi.")
            except Exception:
                logger.info("Follow diklik, lanjut.")
            return True
        except Exception as exc:
            logger.error("IG follow error: %s", exc)
            return None
        finally:
            ig.close()

    def follow_on_instagram(self, ig_url: str) -> bool:
        result = self._retry(lambda: self._try_follow(ig_url), "Follow IG")
        if result:
            count = increment_daily_count("follow")
            logger.info("Follow harian: %d/%d", count, self.cfg.daily_follow_limit)
        return bool(result)

    # ── aksi Instagram: like ─────────────────────────────────────

    _LIKE_UNSET = "svg[aria-label='Like'], svg[aria-label='Suka']"
    _LIKE_SET = "svg[aria-label='Unlike'], svg[aria-label='Batalkan suka']"

    def _try_like(self, ig_url: str) -> bool | None:
        ig = self.context.new_page()
        try:
            ig.goto(ig_url, wait_until="domcontentloaded", timeout=self.cfg.timeout_navigation)
            try:
                ig.locator(f"{self._LIKE_UNSET}, {self._LIKE_SET}").first.wait_for(
                    state="visible", timeout=self.cfg.timeout_element
                )
            except Exception:
                pass

            self._dismiss_ig_popups(ig)

            # Sudah di-like?
            if self._is_visible_on(ig, self._LIKE_SET, self.cfg.timeout_fast):
                logger.info("Post sudah di-like.")
                return True

            like = ig.locator(self._LIKE_UNSET).first
            try:
                like.click(timeout=self.cfg.timeout_element)
                logger.info("Like diklik.")
            except Exception:
                # Fallback: klik ancestor button/div yang bisa diklik
                try:
                    btn = like.locator(
                        "xpath=ancestor::*[self::button or @role='button'][1]"
                    ).first
                    btn.click(timeout=self.cfg.timeout_element)
                    logger.info("Like diklik (ancestor).")
                except Exception:
                    logger.warning("Tombol Like tidak ditemukan.")
                    return None

            self._sleep(self.cfg.delay_after_action)
            self._dismiss_ig_popups(ig)

            if self._is_visible_on(ig, self._LIKE_SET, self.cfg.timeout_fast):
                logger.info("Like dikonfirmasi.")
            else:
                logger.info("Like diklik, lanjut tanpa konfirmasi UI.")
            return True
        except Exception as exc:
            logger.error("IG like error: %s", exc)
            return None
        finally:
            ig.close()

    def like_on_instagram(self, ig_url: str) -> bool:
        result = self._retry(lambda: self._try_like(ig_url), "Like IG")
        if result:
            count = increment_daily_count("like")
            logger.info("Like harian: %d/%d", count, self.cfg.daily_like_limit)
        return bool(result)

    def do_ig_action(self, kind: str, ig_url: str) -> bool:
        return self.like_on_instagram(ig_url) if kind == "like" else self.follow_on_instagram(ig_url)

    # ── verifikasi & next ────────────────────────────────────────

    def _try_verify(self) -> bool | None:
        btn = self.page.locator("text=/Periksa/i").first
        try:
            if self._js_click(btn) or btn.click(timeout=self.cfg.timeout_element):
                logger.info("Periksa diklik.")
                self._settle()
                return True
        except Exception:
            pass
        return None

    def verify_task(self) -> bool:
        self._settle()
        return bool(self._retry(self._try_verify, "Verifikasi"))

    def go_next_task(self) -> bool:
        btn = self.page.locator("text=/[Tt]ugas berikutnya/").first
        try:
            self._js_click(btn) or btn.click(timeout=self.cfg.timeout_element)
            logger.info("→ Tugas berikutnya.")
            self._settle()
            return True
        except Exception:
            return False

    # ── eksekusi satu task ───────────────────────────────────────

    def run_one_task(self, task_index: int) -> TaskResult:
        logger.info("═══ Task #%d ═══", task_index + 1)

        if not self._check_session_alive():
            return TaskResult(task_index, "?", "failed", "Sesi expired.")

        state = self.detect_state()
        username = self._read_target_username() or "unknown"
        self._current_kind = self._detect_task_kind()

        if state == PageState.SLOT_FULL:
            self.go_next_task()
            return TaskResult(task_index, username, "skipped_slot", "Slot penuh.")

        if state == PageState.REVIEWING:
            self.go_next_task()
            self._settle()
            state = self.detect_state()
            username = self._read_target_username() or "unknown"
            if state == PageState.SLOT_FULL:
                self.go_next_task()
                return TaskResult(task_index, username, "skipped_slot", "Slot penuh.")

        if state == PageState.TASK_LIST:
            username = self.select_task(index=0)
            if not username:
                return TaskResult(task_index, "?", "skipped", "Tidak ada task.")
            state = self.detect_state()
            if state == PageState.SLOT_FULL:
                self.go_next_task()
                return TaskResult(task_index, username, "skipped_slot", "Slot penuh.")
        else:
            # Task sudah terbuka tanpa select → deteksi kind dari panel
            self._current_kind = self._detect_task_kind()

        kind = self._current_kind
        logger.info("Target: %s | Jenis: %s | State: %s", username, kind, state.value)

        # Like task tapi dimatikan di config → skip
        if kind == "like" and not self.cfg.handle_like_tasks:
            logger.info("Task like dilewati (handle_like_tasks=false).")
            self.go_next_task()
            return TaskResult(task_index, username, "skipped_like",
                              "Task like dilewati.", kind=kind)

        # Daily limit untuk aksi yang akan dijalankan
        limit = self.cfg.limit_for(kind)
        if get_daily_count(kind) >= limit:
            logger.warning("Daily %s limit tercapai (%d).", kind, limit)
            return TaskResult(task_index, username, "limit_reached",
                              f"Daily {kind} limit {limit} tercapai.", kind=kind)

        earning = self._read_earning()

        if state == PageState.READY_TO_START:
            ig_url = self.start_task()
            if ig_url == "SLOT_FULL":
                self.go_next_task()
                return TaskResult(task_index, username, "skipped_slot", "Slot penuh.", kind=kind)
            if not ig_url:
                return TaskResult(task_index, username, "failed", "Link IG tidak ditemukan.", kind=kind)
        elif state in (PageState.STARTED, PageState.READY_TO_CHECK):
            ig_url = self._get_instagram_url()
        else:
            return TaskResult(task_index, username, "failed",
                              f"State tak diharapkan: {state.value}", kind=kind)

        if state != PageState.READY_TO_CHECK:
            if not ig_url:
                return TaskResult(task_index, username, "failed", "Link IG tidak ditemukan.", kind=kind)
            if not self.do_ig_action(kind, ig_url):
                return TaskResult(task_index, username, "failed", f"Gagal {kind}.", kind=kind)

        if not self.verify_task():
            return TaskResult(task_index, username, "failed", "Gagal verifikasi.", kind=kind)

        self._settle()
        try:
            self.page.locator("text=/[Ss]edang ditinjau/").first.is_visible(
                timeout=self.cfg.timeout_fast
            )
        except Exception:
            pass

        return TaskResult(task_index, username, "submitted",
                          "Submitted untuk review.", kind=kind, earning=earning)

    # ── loop utama ───────────────────────────────────────────────

    def run(self, max_tasks: int = 0) -> list[TaskResult]:
        if "pr.social" not in self.page.url:
            self.navigate_to_tasks()
        elif self.detect_state() == PageState.TASK_LIST:
            if "net=instagram" not in self.page.url:
                self.navigate_to_tasks()

        idx = 0
        fails = 0

        try:
            while True:
                if max_tasks > 0 and idx >= max_tasks:
                    logger.info("Batas %d task tercapai.", max_tasks)
                    break
                if fails >= self.cfg.max_consecutive_fails:
                    logger.error("%d kegagalan berturut, berhenti.", fails)
                    break

                result = self.run_one_task(idx)
                self.results.append(result)

                self.stats.total_tasks += 1
                if result.status == "submitted":
                    self.stats.submitted += 1
                    self.stats.total_earning += result.earning
                    fails = 0
                    logger.info("#%d OK: %s %s (+$%.4f)",
                                idx + 1, result.kind, result.username, result.earning)
                elif result.status in ("skipped", "limit_reached"):
                    self.stats.skipped += 1
                    self.stats.results.append(asdict(result))
                    break
                elif result.status == "skipped_slot":
                    self.stats.skipped_slot += 1
                elif result.status == "skipped_like":
                    self.stats.skipped_like += 1
                elif result.status == "failed":
                    self.stats.failed += 1
                    fails += 1
                    self._error_cooldown(fails)

                self.stats.results.append(asdict(result))
                idx += 1

                post_state = self.detect_state()
                if post_state not in (
                    PageState.REVIEWING, PageState.READY_TO_START,
                    PageState.TASK_LIST, PageState.SLOT_FULL,
                ):
                    self.navigate_to_tasks()

                self._sleep(self.cfg.delay_between_tasks)
        except KeyboardInterrupt:
            logger.info("Dihentikan pengguna (Ctrl+C). Menyimpan progres sesi…")
            self.stats.interrupted = True
        finally:
            self.stats.ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._save_session()

        return self.results

    # ── tracking ─────────────────────────────────────────────────

    def _save_session(self) -> None:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = RESULTS_DIR / f"session_{ts}.json"
        try:
            path.write_text(
                json.dumps(asdict(self.stats), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            logger.info("Hasil sesi disimpan: %s", path)
        except Exception as exc:
            logger.warning("Gagal simpan sesi: %s", exc)

    @staticmethod
    def load_all_sessions() -> list[dict]:
        sessions = []
        for f in sorted(RESULTS_DIR.glob("session_*.json")):
            try:
                sessions.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
        return sessions

    @staticmethod
    def aggregate_stats(sessions: list[dict]) -> dict:
        total = {
            "sessions": len(sessions),
            "total_tasks": 0, "submitted": 0, "failed": 0,
            "skipped": 0, "skipped_slot": 0, "skipped_like": 0, "total_earning": 0.0,
        }
        for s in sessions:
            for k in ("total_tasks", "submitted", "failed", "skipped", "skipped_slot", "skipped_like"):
                total[k] += s.get(k, 0)
            total["total_earning"] += s.get("total_earning", 0.0)
        return total


# ── entry point ──────────────────────────────────────────────────

def run_live_tasks(*, max_tasks: int = 0, background: bool = False,
                   config: ExecutorConfig | None = None) -> list[TaskResult]:
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise ValueError("Pasang Playwright: pip install -r requirements-playwright.txt") from None

    from live_monitor import _connect_cdp, _ensure_browser_running, cleanup_browser

    cfg = config or load_executor_config()
    proc = _ensure_browser_running(background=background)

    try:
        with sync_playwright() as pw:
            browser = _connect_cdp(pw)
            try:
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                logger.info("Browser OK. Tab: %s", page.url)

                from live_monitor import _wait_for_turnstile
                if "pr.social" not in page.url:
                    page.goto(TASKS_URL, wait_until="domcontentloaded",
                              timeout=cfg.timeout_navigation)
                _wait_for_turnstile(page)

                results = TaskExecutor(page, ctx, cfg).run(max_tasks=max_tasks)

                ok = sum(1 for r in results if r.status == "submitted")
                fail = sum(1 for r in results if r.status == "failed")
                earning = sum(r.earning for r in results if r.status == "submitted")
                logger.info("Selesai: %d OK, %d gagal, $%.4f earned.", ok, fail, earning)
                return results
            finally:
                pass
    except Error as exc:
        raise ValueError(f"Browser error: {exc}") from None
    finally:
        cleanup_browser(proc)
