"""Telegram messages without a URL (e.g. a forwarded post): the message text is the source.

Stored as a `web` item with `url: telegram://<chat_id>/<message_id>` (SDD §8.2, known v1 edge case).
"""

from __future__ import annotations

from datetime import date

from ..models import FetchedItem, FetchError
from ..queue import Item, parse_iso
from ..textutil import one_line


def fetch(item: Item, ctx) -> FetchedItem:  # noqa: ARG001 - uniform fetcher signature
    text = (item.inline_text or "").strip()
    if not text:
        raise FetchError("telegram item has no stored text", permanent=True, reason="extraction_empty")
    first = text.splitlines()[0]
    captured = parse_iso(item.captured_at)
    return FetchedItem(
        source_type="web",
        url=item.canonical_url,
        title=one_line(first, 80) or "Telegram note",
        body=text,
        published=captured.date() if captured else date.today(),
        extra={"capture": "telegram_text"},
    )
