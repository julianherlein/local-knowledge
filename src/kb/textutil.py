"""Deterministic text helpers. Token counts are estimated at 4 chars/token, which is
close enough for budget cuts and needs no tokenizer dependency."""

from __future__ import annotations

CHARS_PER_TOKEN = 4
CUT_MARKER = "\n\n[… {n} characters omitted from the middle of this source …]\n\n"


def est_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def head(text: str, max_tokens: int) -> str:
    limit = max_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > limit * 0.8 else limit] + " …"


def head_tail(text: str, max_tokens: int, head_share: float = 0.7) -> tuple[str, bool]:
    """Keep the start and the end of `text` within `max_tokens`. Returns (text, was_truncated)."""
    limit = max_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text, False
    marker_room = 80
    keep = max(limit - marker_room, 0)
    h = int(keep * head_share)
    t = keep - h
    omitted = len(text) - h - t
    return text[:h] + CUT_MARKER.format(n=omitted) + (text[-t:] if t else ""), True


def one_line(text: str, max_chars: int | None = None) -> str:
    s = " ".join((text or "").split())
    if max_chars and len(s) > max_chars:
        s = s[: max_chars - 1].rstrip() + "…"
    return s
