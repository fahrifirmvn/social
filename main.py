"""CLI PR.SOCIAL Task Runner."""

import json
import logging
import math
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from cabinet_snapshot import CabinetSnapshot, describe_changes, parse_cabinet_snapshot
from client import DemoClient, TaskKind
from fixture_monitor import open_fixture_page, render_fixture
from follow_workflow import run_follow_demo
from live_monitor import monitor_live
from runner import History, RetryPolicy, Runner

BASE = Path(__file__).resolve().parent
app = typer.Typer(no_args_is_help=True, help="PR.SOCIAL Task Runner v0.1.0 — mode simulasi.")
console = Console()
logger = logging.getLogger("pr_social")


def follow_demo_path(history: History) -> Path:
    return history.path.with_name(f"{history.path.name}.follow-demo.json")


def load_settings() -> tuple[History, Path, float, RetryPolicy]:
    config = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Konfigurasi harus berupa objek JSON.")
    interval = config.get("poll_interval_seconds")
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or interval <= 0
    ):
        raise ValueError("poll_interval_seconds harus berupa angka positif.")
    for key in ("history_path", "log_path"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"{key} harus berupa path yang tidak kosong.")
    history = History(BASE / config["history_path"])
    follow_demo = History(follow_demo_path(history))
    log_path = BASE / config["log_path"]
    config_path = (BASE / "config.json").resolve()
    if len({
        history.path, history.lock_path.resolve(), log_path.resolve(), config_path,
        follow_demo.path, follow_demo.lock_path.resolve(),
    }) != 6:
        raise ValueError("Path riwayat, checkpoint, lock, log, dan konfigurasi harus berbeda.")
    retry = config.get("retry", {})
    if not isinstance(retry, dict):
        raise ValueError("retry harus berupa objek JSON.")
    defaults = RetryPolicy()
    retry_policy = RetryPolicy(
        initial_seconds=retry.get("initial_seconds", defaults.initial_seconds),
        max_seconds=retry.get("max_seconds", defaults.max_seconds),
        max_attempts=retry.get("max_attempts", defaults.max_attempts),
    )
    return history, log_path, interval, retry_policy


@contextmanager
def session(*, write_log: bool = False):
    handlers: list[logging.Handler] = []
    try:
        history_store, log_path, interval, retry_policy = load_settings()
        handlers.append(RichHandler(console=console, show_path=False))
        if write_log:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            handlers.append(file_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in handlers:
            logger.addHandler(handler)
        yield history_store, interval, retry_policy
    except KeyboardInterrupt:
        logger.info("Dihentikan oleh pengguna.")
        raise typer.Exit(code=130) from None
    except (OSError, ValueError, httpx.HTTPError) as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            message = f"Operasi dihentikan: HTTP {exc.response.status_code}."
        elif isinstance(exc, httpx.HTTPError):
            message = f"Operasi gagal: {type(exc).__name__}"
        else:
            message = str(exc)
        if handlers and all(handler in logger.handlers for handler in handlers):
            logger.error("%s", message)
        else:
            typer.echo(message, err=True)
        raise typer.Exit(code=1) from None
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()


def demo_runner(history_store: History) -> Runner:
    return Runner(DemoClient(), history_store, source="demo")


def show_progress(total: int, completed: int, failed: int) -> None:
    processed = completed + failed
    table = Table(title="PR.SOCIAL Task Runner v0.1.0 · Instagram DEMO")
    for label in ("Tersedia", "Berhasil", "Gagal", "Tersisa"):
        table.add_column(label, justify="right")
    table.add_row(*(str(value) for value in (total, completed, failed, total - processed)))
    console.print(table)
    if total:
        console.print(ProgressBar(total=total, completed=processed, width=30))
    percentage = processed / total if total else 0
    console.print(f"Diproses {processed}/{total} task ({percentage:.1%})")


@app.command()
def status() -> None:
    """Tampilkan status antrean demo."""
    with session() as (history_store, _, _):
        tasks, completed, failed, _ = demo_runner(history_store).snapshot()
        show_progress(len(tasks), completed, failed)


@app.command()
def run(
    dry_run: Annotated[bool, typer.Option(help="Pratinjau antrean demo tanpa verifikasi atau penulisan file.")] = False,
    demo: Annotated[bool, typer.Option(help="Simpan hasil verifikasi simulasi ke riwayat demo.")] = False,
    watch: Annotated[bool, typer.Option(help="Periksa kembali antrean setelah setiap siklus (khusus --demo).")] = False,
    flow_demo: Annotated[bool, typer.Option(help="Jalankan satu task Follow pada fixture Playwright offline.")] = False,
    live: Annotated[bool, typer.Option(help="Eksekusi task Instagram secara live via browser CDP.")] = False,
    once: Annotated[bool, typer.Option(help="Batas satu task untuk alur Follow.")] = False,
    max_tasks: Annotated[int, typer.Option(help="Batas jumlah task live (0 = semua tersedia).")] = 0,
    foreground: Annotated[bool, typer.Option(help="Tampilkan browser di layar (default: background/virtual display).")] = False,
    auto_restart: Annotated[int, typer.Option(help="Auto-restart bot kalau crash, maks N kali (0 = off).")] = 0,
) -> None:
    """Proses task secara berurutan."""
    if sum((dry_run, demo, flow_demo, live)) > 1:
        raise typer.BadParameter("Pilih salah satu: --dry-run, --demo, --flow-demo, atau --live.")
    if not (dry_run or demo or flow_demo or live):
        raise typer.BadParameter("Integrasi resmi belum dikonfigurasi. Gunakan --dry-run, --demo, --flow-demo, atau --live.")
    if watch and not demo:
        raise typer.BadParameter("--watch hanya tersedia bersama --demo.")
    if once and not (flow_demo or live):
        raise typer.BadParameter("--once hanya untuk alur Follow.")
    if max_tasks and not live:
        raise typer.BadParameter("--max-tasks hanya untuk mode --live.")
    if foreground and not live:
        raise typer.BadParameter("--foreground hanya untuk mode --live.")
    if auto_restart and not live:
        raise typer.BadParameter("--auto-restart hanya untuk mode --live.")
    with session(write_log=demo or live) as (history_store, interval, retry_policy):
        if live:
            from task_executor import get_daily_count, load_executor_config, run_live_tasks
            cfg = load_executor_config()
            effective_max = 1 if once else max_tasks
            bg = not foreground
            follows = get_daily_count("follow")
            likes = get_daily_count("like")
            console.print(
                f"Mode live{' (foreground)' if foreground else ''} · "
                f"{'1 task' if once else f'{max_tasks} task' if max_tasks else 'semua task'}"
                f" · follow {follows}/{cfg.daily_follow_limit}"
                f" · like {likes}/{cfg.daily_like_limit}"
                " · Ctrl+C untuk berhenti"
            )
            follow_maxed = follows >= cfg.daily_follow_limit
            like_maxed = (not cfg.handle_like_tasks) or likes >= cfg.daily_like_limit
            if follow_maxed and like_maxed:
                console.print("[yellow]Limit harian tercapai untuk semua aksi. Coba lagi besok.[/yellow]")
                return

            restarts_left = auto_restart
            attempt = 0
            all_results = []

            while True:
                attempt += 1
                try:
                    results = run_live_tasks(max_tasks=effective_max, background=bg, config=cfg)
                    all_results.extend(results)
                    break  # selesai normal
                except KeyboardInterrupt:
                    # Shutdown anggun — progres sudah disimpan di dalam executor.
                    console.print("\n[yellow]Dihentikan oleh pengguna.[/yellow]")
                    break
                except Exception as exc:
                    logger.error("Bot crash (attempt %d): %s", attempt, exc)
                    if restarts_left > 0:
                        restarts_left -= 1
                        wait = min(30 * attempt, 120)
                        logger.info("Auto-restart dalam %d detik… (sisa %d restart)", wait, restarts_left)
                        time.sleep(wait)
                        continue
                    raise

            # Ringkasan
            table = Table(title="Hasil Eksekusi Live")
            for label in ("#", "Username", "Jenis", "Status", "Earning", "Keterangan"):
                table.add_column(label)
            total_earn = 0.0
            for r in all_results:
                status_text = Text(r.status)
                if r.status == "submitted":
                    status_text.stylize("green")
                    total_earn += r.earning
                elif r.status == "failed":
                    status_text.stylize("red")
                elif r.status in ("skipped_slot", "skipped_like", "limit_reached"):
                    status_text.stylize("yellow")
                earn_str = f"${r.earning:.4f}" if r.earning else "-"
                table.add_row(str(r.task_index + 1), r.username, r.kind, status_text, earn_str, r.message)
            console.print(table)
            submitted = sum(1 for r in all_results if r.status == "submitted")
            console.print(f"Total: {submitted} submitted, ${total_earn:.4f} earned.")
            return
        if flow_demo:
            checkpoint, resumed = run_follow_demo(follow_demo_path(history_store))
            if checkpoint is None:
                console.print("DEMO Follow · tidak ada task dengan kuota.")
            else:
                phase = "queued (tertunda, bukan hasil final)" if checkpoint.phase == "queued" else checkpoint.phase
                console.print(f"DEMO Follow · {checkpoint.task_id}: {phase}.", markup=False)
                if resumed:
                    console.print("Checkpoint sudah ada; Start, Follow, dan Cek tidak diulang.")
            return
        if watch:
            console.print("Mode sequential · Ctrl+C untuk berhenti")
        # Simpan progress untuk ringkasan akhir, bukan mencetak ulang tabel tiap task.
        latest = (0, 0, 0)

        def record_progress(total: int, completed: int, failed: int) -> None:
            nonlocal latest
            latest = (total, completed, failed)
            if watch:
                logger.info("Diproses %d/%d task.", completed + failed, total)

        demo_runner(history_store).run(
            dry_run=dry_run, watch=watch, interval=interval, progress=record_progress,
            retry_policy=retry_policy,
        )
        show_progress(*latest)


def monitor_fixture(path: Path, interval: float, once: bool) -> None:
    last_ready_content: bytes | None = None
    with open_fixture_page() as page:
        while True:
            content = path.read_bytes()
            if once or content != last_ready_content:
                tasks = render_fixture(page, content)
                if tasks is None:
                    last_ready_content = None
                    logger.info("Fixture masih dimuat; jumlah task belum diketahui.")
                else:
                    last_ready_content = content
                    table = Table(title="Instagram · FIXTURE LOKAL")
                    for label in ("Tersedia", "Follow", "Like"):
                        table.add_column(label, justify="right")
                    table.add_row(
                        str(len(tasks)),
                        str(sum(task.kind is TaskKind.FOLLOW for task in tasks)),
                        str(sum(task.kind is TaskKind.LIKE for task in tasks)),
                    )
                    console.print(table)
            if once:
                return
            time.sleep(interval)


@app.command()
def monitor(
    fixture: Annotated[Path | None, typer.Option(
        exists=True, dir_okay=False, readable=True,
        help="Baca file HTML lokal lewat Playwright; tidak mengakses situs.",
    )] = None,
    snapshot: Annotated[Path | None, typer.Option(
        exists=True, dir_okay=False, readable=True,
        help="Baca snapshot JSON PR.SOCIAL yang disanitasi, tanpa koneksi live.",
    )] = None,
    live: Annotated[bool, typer.Option(help="Pantau browser PR.SOCIAL yang kamu operasikan sendiri.")] = False,
    confirm_authorized: Annotated[bool, typer.Option(
        help="Konfirmasi bahwa pemantauan browser diizinkan untuk sesi ini.",
    )] = False,
    once: Annotated[bool, typer.Option(help="Periksa satu kali tanpa polling.")] = False,
) -> None:
    """Pantau antrean demo, data lokal, atau browser live baca-saja."""
    if sum((fixture is not None, snapshot is not None, live)) > 1:
        raise typer.BadParameter("Pilih salah satu: --fixture, --snapshot, atau --live.")
    if live and not confirm_authorized:
        raise typer.BadParameter("Gunakan --confirm-authorized hanya jika pemantauan browser diizinkan.")
    if confirm_authorized and not live:
        raise typer.BadParameter("--confirm-authorized hanya digunakan bersama --live.")
    with session(write_log=fixture is None and snapshot is None and not live) as (history_store, interval, _):
        if live:
            console.print(
                "Browser live · Login dan pilih filter Instagram secara manual. "
                "Jika akses dibatasi atau muncul CAPTCHA, hentikan monitor dengan Ctrl+C."
            )
            previous: CabinetSnapshot | None = None

            def show_live(state: CabinetSnapshot) -> None:
                nonlocal previous
                if state != previous:
                    show_cabinet(state, previous, label="LIVE BACA-SAJA")
                    previous = state

            monitor_live(show_live, show_check, once=once)
            return
        if not once:
            console.print("Memantau · Ctrl+C untuk berhenti")
        if fixture is not None:
            monitor_fixture(fixture, interval, once)
            return
        if snapshot is not None:
            monitor_snapshot(snapshot, interval, once)
            return
        runner = demo_runner(history_store)
        while True:
            tasks, completed, failed, _ = runner.snapshot()
            show_progress(len(tasks), completed, failed)
            if once:
                return
            time.sleep(interval)


def show_check(message: str) -> None:
    console.print(f"Verifikasi: {message}")


def show_cabinet(state: CabinetSnapshot, previous: CabinetSnapshot | None, *, label: str) -> None:
    table = Table(title=f"Antrean Instagram PR.SOCIAL · {label}")
    for column in ("Total daftar", "Follow", "Like", "Halaman", "Baris / kapasitas"):
        table.add_column(column, justify="right")
    table.add_row(
        str(state.total), str(state.follow), str(state.like),
        f"{state.page}/{state.pages}", f"{len(state.rows)}/{state.per_page}",
    )
    console.print(table)
    if state.rows:
        quotas = Table(title="Kuota task di halaman ini")
        quotas.add_column("ID", justify="right")
        quotas.add_column("Jenis")
        quotas.add_column("Sisa kuota", justify="right")
        for row in state.rows:
            quotas.add_row(str(row.id), Text(row.action), str(row.left) if row.left else "0 (habis)")
        console.print(quotas)
    else:
        console.print("Tidak ada baris pada halaman ini.")
    if previous is not None:
        for change in describe_changes(previous, state):
            console.print(change)
    if state.verification is not None:
        show_check(state.verification)


def monitor_snapshot(path: Path, interval: float, once: bool) -> None:
    last_content: bytes | None = None
    previous: CabinetSnapshot | None = None
    while True:
        content = path.read_bytes()
        if content != last_content:
            state = parse_cabinet_snapshot(content)
            show_cabinet(state, previous, label="SNAPSHOT LOKAL")
            previous = state
            last_content = content
        if once:
            return
        time.sleep(interval)


@app.command()
def history() -> None:
    """Tampilkan riwayat task lokal."""
    with session() as (history_store, _, _):
        records = history_store.load()
        if not records:
            console.print("Belum ada riwayat.")
            return
        table = Table(title="Riwayat task")
        for label in ("ID", "Jenis", "Hasil", "Sumber", "Waktu (UTC)"):
            table.add_column(label)
        for record in records:
            table.add_row(*(Text(record[key]) for key in ("task_id", "kind", "result", "source", "processed_at")))
        console.print(table)


@app.command()
def stats() -> None:
    """Tampilkan statistik dari semua sesi live."""
    from task_executor import TaskExecutor
    sessions = TaskExecutor.load_all_sessions()
    if not sessions:
        console.print("Belum ada data sesi. Jalankan: main.py run --live")
        return

    agg = TaskExecutor.aggregate_stats(sessions)

    table = Table(title="Statistik Live (semua sesi)")
    for label in ("Total Sesi", "Total Task", "Submitted", "Gagal", "Slot Penuh", "Like Dilewati", "Dilewati", "Total Earning"):
        table.add_column(label, justify="right")
    table.add_row(
        str(agg["sessions"]), str(agg["total_tasks"]),
        str(agg["submitted"]), str(agg["failed"]),
        str(agg["skipped_slot"]), str(agg["skipped_like"]), str(agg["skipped"]),
        f"${agg['total_earning']:.4f}",
    )
    console.print(table)

    # Tabel per-sesi
    detail = Table(title="Detail per sesi")
    for label in ("Waktu", "Task", "OK", "Gagal", "Earning"):
        detail.add_column(label, justify="right")
    for s in sessions[-10:]:  # 10 sesi terakhir
        detail.add_row(
            s.get("started_at", "?")[:19],
            str(s.get("total_tasks", 0)),
            str(s.get("submitted", 0)),
            str(s.get("failed", 0)),
            f"${s.get('total_earning', 0):.4f}",
        )
    console.print(detail)


if __name__ == "__main__":
    app()
