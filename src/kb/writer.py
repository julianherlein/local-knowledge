"""Renders raw source files (SDD §7.4), source summaries (§7.5) and compile-queue lines."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from . import fm
from .models import FetchedItem, SummaryResult, TagResult
from .queue import Item, parse_iso
from .summarizer import render_body
from .textutil import one_line
from .vault import COMPILE_QUEUE, RAW_DIRS, SOURCES_DIR

COMPILE_QUEUE_HEADER = """---
type: compile-queue
tags: [meta]
---
# Compile queue

Sources summarized but not yet compiled into concept/entity pages. Compile sessions
tick items (`- [x]`) as they process them. Filter by domain with the hashtag.

"""


def raw_rel(item_source_type: str, stem: str) -> str:
    return f"{RAW_DIRS[item_source_type]}/{stem}.md"


def summary_rel(stem: str) -> str:
    return f"{SOURCES_DIR}/{stem}.md"


def domain_tags(domains: list[str]) -> list[str]:
    return list(domains)


def raw_meta(item: Item, fetched: FetchedItem, tagged: TagResult) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "id": item.id,
        "title": fetched.title,
        "source_type": fetched.source_type,
        "url": fetched.url,
        "author": fetched.author,
        "published": fetched.published,
        "captured": parse_iso(item.captured_at),
        "origin": item.origin,
        "domains": tagged.domains,
        "tag_method": tagged.method,
        "language": tagged.language or fetched.language,
    }
    if fetched.source_type == "x":
        meta["thread"] = fetched.thread or "complete"
    meta["outlinks"] = fetched.outlinks
    for k, v in fetched.extra.items():
        if k not in meta:
            meta[k] = v
    if item.note:
        meta["note"] = item.note
    meta["tags"] = ["raw", *domain_tags(tagged.domains)]
    return {k: v for k, v in meta.items() if v is not None}


def render_raw(item: Item, fetched: FetchedItem, tagged: TagResult) -> str:
    return fm.render(raw_meta(item, fetched, tagged), fetched.body)


def summary_meta(raw_rel_path: str, meta: dict[str, Any], item_id: int, truncated: bool) -> dict[str, Any]:
    domains = meta.get("domains") or []
    out: dict[str, Any] = {
        "source": f"[[{raw_rel_path.removesuffix('.md')}]]",
        "type": "source-summary",
        "status": "uncompiled",
        "item_id": item_id,
        "source_type": meta.get("source_type"),
        "url": meta.get("url"),
        "author": meta.get("author"),
        "published": meta.get("published"),
        "captured": _captured_date(meta.get("captured")),
        "domains": domains,
    }
    if truncated:
        out["truncated"] = True
    out["tags"] = ["source", *domain_tags(domains)]
    return {k: v for k, v in out.items() if v is not None}


def _captured_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def render_summary(
    raw_rel_path: str, meta: dict[str, Any], item_id: int, summary: SummaryResult, truncated: bool
) -> str:
    return fm.render(summary_meta(raw_rel_path, meta, item_id, truncated), render_body(summary))


def compile_queue_line(stem: str, domains: list[str], tldr: str) -> str:
    tags = " ".join(f"#{d}" for d in domains)
    return f"- [ ] [[sources/{stem}]] {tags} — {one_line(tldr, 240)}"


def append_compile_queue(existing: str | None, stem: str, domains: list[str], tldr: str) -> str | None:
    """New file content with the line appended, or None if this source is already queued."""
    text = existing if existing is not None else COMPILE_QUEUE_HEADER
    if re.search(rf"\[\[sources/{re.escape(stem)}(\|[^\]]*)?\]\]", text):
        return None
    if not text.endswith("\n"):
        text += "\n"
    return text + compile_queue_line(stem, domains, tldr) + "\n"


def retag_compile_queue(text: str, stem: str, domains: list[str]) -> str:
    """Rewrite the hashtags on this source's queue line (used by `kb tag`)."""
    pat = re.compile(rf"^(- \[[ xX]\] \[\[sources/{re.escape(stem)}\]\])((?: #[\w-]+)*)( — .*)$", re.MULTILINE)
    return pat.sub(lambda m: m.group(1) + "".join(f" #{d}" for d in domains) + m.group(3), text)


__all__ = [
    "COMPILE_QUEUE",
    "append_compile_queue",
    "compile_queue_line",
    "raw_meta",
    "raw_rel",
    "render_raw",
    "render_summary",
    "retag_compile_queue",
    "summary_rel",
]
