"""`kb digest`: daily and weekly digest notes built from the DB only (SDD §10). (Stub.)"""

from __future__ import annotations

from datetime import date

from .config import Settings
from .queue import Queue


def run(
    settings: Settings, queue: Queue, *, day: date | None = None, week: str | None = None, commit: bool = True
) -> list[str]:
    raise NotImplementedError("digest")
