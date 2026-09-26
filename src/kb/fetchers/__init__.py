"""Fetchers turn a queued item into a FetchedItem (SDD §7.2).

Contract for every fetcher module: `fetch(item: Item, ctx: FetchContext) -> FetchedItem`.
They raise `FetchError(permanent=..., reason=...)` for expected failures and must not
let raw httpx/yt-dlp exceptions escape (the pipeline treats unknown exceptions as
transient, which is right for bugs but wastes retries on a 404).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import Settings
from ..models import FetchedItem
from ..normalize import TELEGRAM_SCHEME
from ..queue import Item, Queue


@dataclass
class FetchContext:
    settings: Settings
    http: httpx.Client
    queue: Queue


def fetch(item: Item, ctx: FetchContext) -> FetchedItem:
    if item.canonical_url.startswith(TELEGRAM_SCHEME):
        from . import inline

        return inline.fetch(item, ctx)
    if item.source_type == "x":
        from . import x

        return x.fetch(item, ctx)
    if item.source_type == "youtube":
        from . import youtube

        return youtube.fetch(item, ctx)
    from . import web

    return web.fetch(item, ctx)
