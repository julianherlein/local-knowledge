"""Capture adapters (SDD §8). Each `poll()` enqueues new items and returns a CaptureReport."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ..config import Settings
from ..queue import Queue


@dataclass
class CaptureContext:
    settings: Settings
    http: httpx.Client
    queue: Queue


@dataclass
class CaptureReport:
    source: str
    enqueued: int = 0
    duplicates: int = 0
    seen: int = 0
    skipped: bool = False
    """True when the adapter is disabled or not configured (not an error)."""
    message: str = ""
    errors: list[str] = field(default_factory=list)
