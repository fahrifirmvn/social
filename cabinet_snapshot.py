"""Pembaca snapshot lokal yang disanitasi; tidak memanggil endpoint live."""

import json
from dataclasses import dataclass

from client import INSTAGRAM


@dataclass(frozen=True)
class CabinetRow:
    id: int
    action: str
    left: int


@dataclass(frozen=True)
class CabinetSnapshot:
    total: int
    follow: int
    like: int
    page: int
    pages: int
    per_page: int
    filters: tuple[str | None, ...]
    rows: tuple[CabinetRow, ...]
    verification: str | None


def describe_changes(before: CabinetSnapshot, after: CabinetSnapshot) -> list[str]:
    if (before.page, before.per_page, before.filters) != (after.page, after.per_page, after.filters):
        return []  # Jangan bandingkan baris dari halaman atau filter yang berbeda.

    changes = []
    if before.total != after.total:
        changes.append(f"Total daftar: {before.total} → {after.total}.")
    old_rows = {row.id: row for row in before.rows}
    new_ids = {row.id for row in after.rows}
    for row in after.rows:
        old = old_rows.get(row.id)
        if old is None:
            changes.append(f"#{row.id} muncul di halaman (sisa kuota {row.left}).")
        elif old.left != row.left:
            changes.append(f"#{row.id} sisa kuota: {old.left} → {row.left}.")
    for row in before.rows:
        if row.id not in new_ids:
            changes.append(f"#{row.id} tidak lagi terlihat di halaman ini.")
    return changes


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"Snapshot: {name} harus berupa objek JSON.")
    return value


def _count(fields: dict, key: str) -> int:
    value = fields.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"Snapshot: {key} harus berupa bilangan bulat tidak negatif.")
    return value


def parse_verification_status(payload: object) -> str:
    check = _object(payload, "check")
    if (
        check.get("ok") is True
        and check.get("eligible") is True
        and "found" in check
        and check["found"] is None
        and check.get("code") == "queued"
    ):
        wait = _count(check, "wait_min")
        return f"Tertunda (queued); PR.SOCIAL melaporkan waktu tunggu {wait} menit."
    return "Belum bisa dipastikan; tidak dianggap sebagai hasil final."


def parse_cabinet_snapshot(content: bytes) -> CabinetSnapshot:
    snapshot = _object(json.loads(content), "snapshot")
    verification = parse_verification_status(snapshot["check"]) if "check" in snapshot else None
    return parse_cabinet_listing(snapshot.get("listing"), verification=verification)


def parse_cabinet_listing(payload: object, *, verification: str | None = None) -> CabinetSnapshot:
    listing = _object(payload, "listing")
    if listing.get("ok") is not True or listing.get("state") != "ok":
        raise ValueError("Snapshot: daftar task belum siap atau gagal dimuat.")
    queue = _object(listing.get("list"), "listing.list")
    if queue.get("net") != INSTAGRAM:
        raise ValueError("Snapshot harus difilter ke Instagram agar total dan halaman sesuai.")
    types = _object(queue.get("types"), "listing.list.types")
    raw_rows = queue.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError("Snapshot: rows harus berupa daftar objek.")
    per_page = _count(queue, "per")
    if len(raw_rows) > per_page:
        raise ValueError("Snapshot: jumlah baris melebihi kapasitas halaman.")
    rows = []
    ids: set[int] = set()
    for raw_row in raw_rows:
        row = _object(raw_row, "baris task")
        if row.get("platform") != INSTAGRAM:
            raise ValueError("Snapshot Instagram tidak boleh berisi baris dari platform lain atau tanpa platform.")
        task_id = _count(row, "id")
        if task_id in ids:
            raise ValueError("Snapshot: ID task muncul lebih dari sekali pada halaman.")
        action = row.get("action")
        if not isinstance(action, str) or not action.strip():
            raise ValueError("Snapshot: action harus berupa teks yang tidak kosong.")
        rows.append(CabinetRow(task_id, action.strip(), _count(row, "left")))
        ids.add(task_id)

    filters = tuple(queue.get(key) for key in ("net", "action", "sort"))
    if any(value is not None and not isinstance(value, str) for value in filters):
        raise ValueError("Snapshot: filter halaman harus berupa teks.")

    return CabinetSnapshot(
        total=_count(queue, "total"),
        follow=_count(types, "follow"),
        like=_count(types, "like"),
        page=_count(queue, "page"),
        pages=_count(queue, "pages"),
        per_page=per_page,
        filters=filters,
        rows=tuple(rows),
        verification=verification,
    )
