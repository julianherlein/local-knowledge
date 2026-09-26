"""Fetcher for x sources. See SDD §7.2. (Stub: implemented by the x builder.)"""

from __future__ import annotations

from ..models import FetchedItem
from ..queue import Item


def fetch(item: Item, ctx) -> FetchedItem:
    raise NotImplementedError("x fetcher")
