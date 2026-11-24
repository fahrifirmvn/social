"""Eksekusi task secara berurutan dan penyimpanan riwayat lokal."""

import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from client import INSTAGRAM, Task, TaskClient, Verification

logger = logging.getLogger("pr_social")
TASK_GAP_SECONDS = 3


class HistoryBusyError(ValueError):
    """Runner lain sedang memegang lock untuk riwayat yang sama."""


def write_json_atomically(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f"{path.name}.", suffix=".tmp", delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class History:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    @contextmanager
    def write_lock(self):
        try:
            import fcntl
        except ImportError:
            raise ValueError("Mode tulis memerlukan dukungan flock (Linux/macOS).") from None

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Kernel melepas lock saat descriptor ditutup. Jangan hapus file lock:
        # semua runner harus membuka inode yang sama.
        with self.lock_path.open("a") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HistoryBusyError("Runner lain sedang memakai riwayat ini.") from None
            yield

    def load(self) -> list[dict[str, str]]:
        try:
            records = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        if not isinstance(records, list) or any(
            not isinstance(record, dict)
            or not all(
                isinstance(record.get(key), str)
                for key in ("task_id", "kind", "result", "processed_at", "source")
            )
            or record["result"] not in ("verified", "failed")
            for record in records
        ):
            raise ValueError("Format riwayat tidak valid.")
        return records

    def append(
        self, records: list[dict[str, str]], task: Task, result: Verification, source: str,
    ) -> None:
        record = {
            "task_id": task.id,
            "kind": task.kind.value,
            "result": result.value,
            "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source,
        }
        write_json_atomically(self.path, [*records, record])
        records.append(record)


def unique_tasks(tasks: Iterable[Task]) -> list[Task]:
    by_id: dict[str, Task] = {}
    for task in tasks:
        by_id.setdefault(task.id, task)
    return list(by_id.values())


@dataclass(frozen=True)
class RetryPolicy:
    initial_seconds: float = 30
    max_seconds: float = 300
    max_attempts: int = 4

    def __post_init__(self) -> None:
        if (
            any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in (self.initial_seconds, self.max_seconds)
            )
            or not 0 < self.initial_seconds <= self.max_seconds < math.inf
        ):
            raise ValueError("retry memerlukan 0 < initial_seconds <= max_seconds (berhingga).")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError("retry.max_attempts harus berupa bilangan bulat positif.")


@dataclass(frozen=True)
class DeferredTask:
    attempts: int
    delay: float
    not_before: float | None  # None berarti batas percobaan sudah tercapai.


def is_retryable_verification_error(error: httpx.HTTPError) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in (408, 500, 502, 503, 504)
    return isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))


class Runner:
    def __init__(self, client: TaskClient, history: History, *, source: str) -> None:
        self.client = client
        self.history = history
        self.source = source

    def _index_results(self, records: list[dict[str, str]]) -> dict[str, str]:
        return {row["task_id"]: row["result"] for row in records if row["source"] == self.source}

    def snapshot(self) -> tuple[list[Task], int, int, list[Task]]:
        return self._snapshot(self._index_results(self.history.load()))

    def _snapshot(self, results: dict[str, str]) -> tuple[list[Task], int, int, list[Task]]:
        logger.info("Memeriksa ketersediaan task Instagram (%s)…", self.source)
        tasks = unique_tasks(task for task in self.client.list_tasks() if task.platform == INSTAGRAM)
        completed = sum(results.get(task.id) == Verification.VERIFIED for task in tasks)
        failed = sum(results.get(task.id) == Verification.FAILED for task in tasks)
        remaining = [task for task in tasks if task.id not in results]
        logger.info("Ditemukan %d task; %d belum diproses.", len(tasks), len(remaining))
        return tasks, completed, failed, remaining

    def run(
        self,
        *,
        dry_run: bool,
        watch: bool,
        interval: float,
        progress: Callable[[int, int, int], None],
        retry_policy: RetryPolicy = RetryPolicy(),
    ) -> None:
        with nullcontext() if dry_run else self.history.write_lock():
            self._run(dry_run, watch, interval, progress, retry_policy)

    def _run(
        self,
        dry_run: bool,
        watch: bool,
        interval: float,
        progress: Callable[[int, int, int], None],
        retry_policy: RetryPolicy,
    ) -> None:
        records = self.history.load()
        results = self._index_results(records)
        deferred: dict[str, DeferredTask] = {}
        next_start_at: float | None = None
        while True:
            tasks, completed, failed, remaining = self._snapshot(results)
            progress(len(tasks), completed, failed)
            for task in remaining:
                if dry_run:
                    logger.info("DRY RUN #%s — %s", task.id, task.kind.value)
                    continue
                previous = deferred.get(task.id)
                if previous is not None and (
                    previous.not_before is None or time.monotonic() < previous.not_before
                ):
                    continue
                if next_start_at is not None:
                    delay = next_start_at - time.monotonic()
                    if delay > 0:
                        logger.info("Menunggu %.1f detik sebelum task berikutnya.", delay)
                        time.sleep(delay)
                    next_start_at = None
                logger.info("TASK #%s — %s", task.id, task.kind.value)
                try:
                    result = self.client.verify_task(task)
                except httpx.HTTPError as exc:
                    if not is_retryable_verification_error(exc):
                        raise
                    result = Verification.PENDING
                    reason = (
                        f"HTTP {exc.response.status_code}"
                        if isinstance(exc, httpx.HTTPStatusError)
                        else type(exc).__name__
                    )
                else:
                    reason = "sumber belum mengonfirmasi hasil"
                if result is Verification.PENDING:
                    if watch:
                        deferred[task.id] = self._schedule_retry(task, previous, retry_policy, reason)
                    else:
                        logger.info("Verifikasi #%s tertunda (%s); lanjut ke task berikutnya.", task.id, reason)
                    continue
                self.history.append(records, task, result, self.source)
                next_start_at = time.monotonic() + TASK_GAP_SECONDS
                results[task.id] = result.value
                deferred.pop(task.id, None)
                if result is Verification.VERIFIED:
                    completed += 1
                    logger.info("OK #%s — verifikasi berhasil (%s).", task.id, self.source)
                else:
                    failed += 1
                    logger.error("Verifikasi #%s gagal (%s); lanjut ke task berikutnya.", task.id, self.source)
                progress(len(tasks), completed, failed)
            if not watch or dry_run:
                return
            logger.info("Siklus selesai; cek kembali dalam %g detik.", interval)
            time.sleep(interval)

    @staticmethod
    def _schedule_retry(
        task: Task, previous: DeferredTask | None, policy: RetryPolicy, reason: str,
    ) -> DeferredTask:
        attempts = previous.attempts + 1 if previous else 1
        if attempts >= policy.max_attempts:
            logger.warning(
                "Verifikasi #%s tetap tertunda (%s); batas %d percobaan tercapai untuk sesi ini.",
                task.id, reason, attempts,
            )
            return DeferredTask(attempts, 0, None)

        delay = min(previous.delay * 2, policy.max_seconds) if previous else policy.initial_seconds
        not_before = time.monotonic() + delay
        logger.info("Verifikasi #%s tertunda (%s); cek ulang paling cepat %g detik.", task.id, reason, delay)
        return DeferredTask(attempts, delay, not_before)
