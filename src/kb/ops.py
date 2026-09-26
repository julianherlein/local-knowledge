"""Maintenance operations behind `kb tag`, `kb status` and `kb retry`."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import fm, writer
from .config import Settings
from .git_ops import Tracker
from .queue import Queue, parse_iso
from .tagger import UNSORTED
from .vault import COMPILE_QUEUE, Vault


class OpError(RuntimeError):
    pass


def retag(settings: Settings, queue: Queue, item_id: int, domains: list[str], *, commit: bool = True) -> list[str]:
    """Set an item's domains. Rewrites front matter only (raw body untouched); no files move."""
    valid = set(settings.domain_names) | {UNSORTED}
    domains = list(dict.fromkeys(d.lstrip("#").lower() for d in domains))
    mapping = settings.hashtag_map()
    domains = [mapping.get(d, d) for d in domains]
    bad = [d for d in domains if d not in valid]
    if bad or not domains:
        raise OpError(f"unknown domain(s): {', '.join(bad) or '(none given)'}; valid: {', '.join(sorted(valid))}")
    if len(domains) > 2:
        raise OpError("at most 2 domains per item (primary first)")
    item = queue.get(item_id)
    if item is None:
        raise OpError(f"no item {item_id}")

    tracker = Tracker(settings.vault_path, queue)
    vault = Vault(settings.vault_path, tracker)
    changed: list[str] = []
    for rel, type_tag in ((item.raw_path, "raw"), (item.summary_path, "source")):
        if rel and vault.exists(rel):
            text = vault.read(rel)
            meta, _ = fm.split(text)
            other_tags = [t for t in meta.get("tags", []) if t not in (type_tag, UNSORTED, *settings.domain_names)]
            updates = {"domains": domains, "tags": [type_tag, *domains, *other_tags]}
            if type_tag == "raw":
                updates["tag_method"] = "manual"
            vault.write(rel, fm.replace_front_matter(text, updates), item.id)
            changed.append(rel)
    if item.summary_path and vault.exists(COMPILE_QUEUE):
        stem = item.summary_path.rsplit("/", 1)[-1].removesuffix(".md")
        text = vault.read(COMPILE_QUEUE)
        new = writer.retag_compile_queue(text, stem, domains)
        if new != text:
            vault.write(COMPILE_QUEUE, new, item.id)
            changed.append(COMPILE_QUEUE)
    queue.update(item_id, domains=domains, tag_method="manual", tag_confidence=None)
    if commit and changed:
        tracker.commit(prefix="auto: retag")
    return changed


_QUEUE_LINE = re.compile(r"^- \[ \] \[\[sources/[^\]]+\]\]((?: #[\w-]+)*)", re.MULTILINE)


def compile_queue_counts(settings: Settings) -> Counter[str]:
    """Unchecked compile-queue lines per primary domain."""
    path = settings.vault_path / COMPILE_QUEUE
    counts: Counter[str] = Counter()
    if not path.exists():
        return counts
    for m in _QUEUE_LINE.finditer(path.read_text(encoding="utf-8")):
        tags = m.group(1).split()
        counts[tags[0].lstrip("#") if tags else UNSORTED] += 1
    return counts


@dataclass
class Status:
    counts: dict[str, int]
    errors: list = field(default_factory=list)
    unsorted: list = field(default_factory=list)
    compile_queue: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    llm_calls_30d: int = 0
    llm_cost_30d: float = 0.0
    pending_git: list[str] = field(default_factory=list)


def status(settings: Settings, queue: Queue) -> Status:
    st = Status(counts=queue.counts(), errors=queue.recent_errors(10))
    st.unsorted = [i for i in queue.all_items() if UNSORTED in i.domains and i.status == "done"]
    st.compile_queue = compile_queue_counts(settings)
    since = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
    st.llm_calls_30d, st.llm_cost_30d = queue.llm_cost_since(since)

    if settings.telegram.enabled and settings.telegram_bot_token:
        last = parse_iso(queue.get_state("telegram.last_poll_at"))
        if last is None:
            st.warnings.append("Telegram has never been polled. Run `kb run`.")
        else:
            age_h = (datetime.now().astimezone() - last).total_seconds() / 3600
            if age_h > settings.telegram.stale_warning_hours:
                st.warnings.append(
                    f"Last Telegram poll was {age_h:.0f}h ago. Telegram drops updates after ~24h; run `kb run` now."
                )
    if (settings.vault_path / ".git").exists():
        import json

        st.pending_git = sorted(json.loads(queue.get_state("git.pending") or "{}"))
    elif settings.vault_path.exists():
        st.warnings.append(f"{settings.vault_path} is not a git repo; run `kb init`.")
    else:
        st.warnings.append(f"vault {settings.vault_path} does not exist; run `kb init`.")
    return st
