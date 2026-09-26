"""Fetcher for web sources. See SDD §7.2. (Stub: implemented by the web builder.)"""

from __future__ import annotations

from ..models import FetchedItem
from ..queue import Item


def fetch(item: Item, ctx) -> FetchedItem:
    raise NotImplementedError("web fetcher")
