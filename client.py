"""Sumber task sinkron. MVP hanya menyediakan data simulasi."""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

INSTAGRAM = "instagram"


class TaskKind(str, Enum):
    FOLLOW = "follow"
    LIKE = "like"


@dataclass(frozen=True)
class Task:
    id: str
    kind: TaskKind
    platform: str = INSTAGRAM


class Verification(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    PENDING = "pending"


class TaskClient(Protocol):
    def list_tasks(self) -> list[Task]: ...

    def verify_task(self, task: Task) -> Verification: ...


class DemoClient:
    """Tidak mengirim request atau melakukan tindakan di media sosial."""

    def list_tasks(self) -> list[Task]:
        return [
            Task(f"{number:03}", TaskKind.FOLLOW if number % 2 else TaskKind.LIKE)
            for number in range(1, 25)
        ]

    def verify_task(self, task: Task) -> Verification:
        return Verification.FAILED if task.id == "003" else Verification.VERIFIED
