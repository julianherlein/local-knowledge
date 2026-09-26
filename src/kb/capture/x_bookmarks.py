"""Capture adapter: x_bookmarks. See SDD §8. (Stub: implemented by the builder.)"""

from __future__ import annotations

from . import CaptureContext, CaptureReport


def poll(ctx: CaptureContext) -> CaptureReport:
    return CaptureReport(source="x_bookmarks", skipped=True, message="not implemented")
