"""`kb digest`: daily and weekly digest notes built from the DB only (SDD §10). No LLM calls.

Scheduling assumption: nobody runs this at 07:00 by cron. The user runs `kb digest` (or
`kb run`) by hand, whenever, so a run must *catch up*:

* Every local date after the last closed day (state `digest.last_closed_day`) through today
  gets its note, if it had ingests or failures. The first run looks back at most 14 days.
* Yesterday is then closed. Today stays open, so a second run later today rewrites today's
  note with whatever arrived since.
* Weekly rollups are written for completed ISO weeks that have no file yet (the previous
  week, plus any completed week the catch-up window touched), instead of "on Monday".

The notes are also Obsidian daily notes (`.obsidian/daily-notes.json` points at
digests/daily), so the file may already exist: empty (Obsidian created it when the user
opened "today"), or with the user's own writing. Generated content lives between
`<!-- kb:digest:start -->` and `<!-- kb:digest:end -->`; only that region and our front
matter keys are ever replaced. User text without markers is kept under `## My notes`.

Dates are local: an item belongs to the local calendar day of its `done_at`, converted to
the machine's current timezone.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import yaml

from . import fm
from .config import Settings
from .git_ops import Tracker
from .init import domain_title
from .ops import compile_queue_counts
from .queue import FAILED, FAILED_PERMANENT, Item, Queue, parse_iso
from .tagger import UNSORTED
from .textutil import one_line
from .vault import COMPILE_QUEUE, DIGEST_DAILY, DIGEST_WEEKLY, Vault

STATE_KEY = "digest.last_closed_day"
FIRST_RUN_LOOKBACK_DAYS = 14
OLDEST_UNCOMPILED = 10
START = "<!-- kb:digest:start -->"
END = "<!-- kb:digest:end -->"
MY_NOTES = "## My notes"
_WEEK = re.compile(r"^(\d{4})-W(\d{1,2})$")
_QUEUE_UNCHECKED = re.compile(r"^- \[ \] \[\[(wiki/sources/[^\]|]+)(?:\|[^\]]*)?\]\]((?: #[\w-]+)*)", re.MULTILINE)


# Time helpers --------------------------------------------------------------------------
def local_day(iso: str | None) -> date | None:
    dt = parse_iso(iso)
    return dt.astimezone().date() if dt else None


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def parse_week(week: str) -> date:
    """`2026-W39` -> the Monday of that ISO week."""
    m = _WEEK.match(week.strip())
    if not m:
        raise ValueError(f"week must look like 2026-W39, got {week!r}")
    try:
        return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError as e:
        raise ValueError(f"no such ISO week: {week}") from e


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


# Data ----------------------------------------------------------------------------------
@dataclass
class Activity:
    """Items bucketed by local day: finished (`done`) and failed (by `updated_at`)."""

    done: dict[date, list[Item]]
    failed: dict[date, list[Item]]
    unsorted: list[Item]
    by_summary: dict[str, Item]

    @classmethod
    def load(cls, queue: Queue) -> Activity:
        done: dict[date, list[Item]] = defaultdict(list)
        failed: dict[date, list[Item]] = defaultdict(list)
        unsorted: list[Item] = []
        by_summary: dict[str, Item] = {}
        for item in queue.all_items():
            if item.summary_path:
                by_summary[item.summary_path.removesuffix(".md")] = item
            if item.status == "done":
                d = local_day(item.done_at)
                if d:
                    done[d].append(item)
                    if UNSORTED in item.domains:
                        unsorted.append(item)
            elif item.status in (FAILED, FAILED_PERMANENT):
                d = local_day(item.updated_at)
                if d:
                    failed[d].append(item)
        return cls(done, failed, unsorted, by_summary)

    def active(self, d: date) -> bool:
        return bool(self.done.get(d) or self.failed.get(d))

    def earliest(self) -> date | None:
        days = [*self.done, *self.failed]
        return min(days) if days else None

    def in_range(self, bucket: dict[date, list[Item]], start: date, end: date) -> list[Item]:
        return [i for d in sorted(bucket) if start <= d <= end for i in bucket[d]]


# Rendering -----------------------------------------------------------------------------
def _alias(text: str) -> str:
    """Link display text: `|`, `[` and `]` would break the wikilink."""
    return one_line(text).replace("|", "-").replace("[", "(").replace("]", ")")


def item_link(item: Item) -> str:
    title = _alias(item.title or item.canonical_url)
    if item.summary_path:
        return f"[[{item.summary_path.removesuffix('.md')}|{title}]]"
    return title


def primary(item: Item) -> str:
    return item.domains[0] if item.domains else UNSORTED


def item_entry(item: Item, with_date: bool = False) -> str:
    extras = [f"({item.source_type})"]
    if len(item.domains) > 1:
        extras.append(" ".join(f"#{d}" for d in item.domains[1:]))
    if with_date and (d := local_day(item.done_at)):
        extras.append(d.isoformat())
    line = f"- **{item_link(item)}** {' '.join(extras)}"
    if item.tldr:
        line += f"\n  {one_line(item.tldr)}"
    return line


def failure_entry(item: Item) -> str:
    what = _alias(item.title) if item.title else item.canonical_url
    reason = item.error_reason or "error"
    line = f"- #{item.id} {what} ({item.source_type}): `{reason}`"
    if item.last_error:
        line += f". {one_line(item.last_error, 200)}"
    if item.status == FAILED_PERMANENT:
        line += f"\n  Gave up. After fixing the cause: `kb retry {item.id}`"
    else:
        line += "\n  Will be retried on the next `kb run`."
    return line


def domain_order(settings: Settings, present: set[str]) -> list[str]:
    configured = [d for d in settings.domain_names if d in present]
    return configured + sorted(present - set(configured) - {UNSORTED})


def grouped_entries(settings: Settings, items: list[Item], with_date: bool = False) -> list[str]:
    """Sections per primary domain (config order), unsorted items excluded (they go to Needs attention)."""
    groups: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        if primary(item) != UNSORTED:
            groups[primary(item)].append(item)
    out: list[str] = []
    for d in domain_order(settings, set(groups)):
        out += [f"### {domain_heading(settings, d)} ({len(groups[d])})", ""]
        out += [*(item_entry(i, with_date) for i in groups[d]), ""]
    return out


def domain_heading(settings: Settings, d: str) -> str:
    """Configured domains link to their hub; a domain dropped from config stays plain text."""
    return f"[[wiki/domains/{d}|{domain_title(d)}]]" if d in settings.domain_names else d


def queue_section(settings: Settings) -> list[str]:
    counts = compile_queue_counts(settings)
    total = sum(counts.values())
    lines = [
        "## Compile queue",
        "",
        f"Unchecked in [[{COMPILE_QUEUE.removesuffix('.md')}]] when this note was written: {total}.",
        "",
    ]
    if total:
        for d in domain_order(settings, set(counts)):
            lines.append(f"- #{d}: {counts[d]}")
        if counts.get(UNSORTED):
            lines.append(f"- #{UNSORTED}: {counts[UNSORTED]} (tag them first)")
        lines.append("")
    return lines


def unsorted_entries(items: list[Item]) -> list[str]:
    out = []
    for item in items:
        out.append(item_entry(item))
        out.append(f"  Pick a domain: `kb tag {item.id} <domain>`")
    return out


def render_daily(settings: Settings, act: Activity, d: date) -> tuple[dict[str, Any], str]:
    done = sorted(act.done.get(d, []), key=lambda i: (i.done_at or "", i.id))
    failed = sorted(act.failed.get(d, []), key=lambda i: (i.updated_at, i.id))
    unsorted = [i for i in act.unsorted if (local_day(i.done_at) or d) <= d]
    meta = {"type": "digest", "date": d, "items": len(done), "tags": ["digest"]}

    lines = [f"# Digest {d.isoformat()}", "", _summary_line(len(done), len(failed)), ""]
    sorted_done = [i for i in done if primary(i) != UNSORTED]
    if sorted_done:
        lines += ["## New in the knowledge base", "", *grouped_entries(settings, sorted_done)]
    if failed or unsorted:
        lines += ["## Needs attention", ""]
        if failed:
            lines += ["### Failed", "", *(failure_entry(i) for i in failed), ""]
        if unsorted:
            lines += ["### Unsorted", "", *unsorted_entries(unsorted), ""]
    lines += queue_section(settings)
    return meta, "\n".join(lines).rstrip("\n")


def _summary_line(n_done: int, n_failed: int) -> str:
    items = f"{n_done} item{'s' if n_done != 1 else ''} ingested"
    fails = f"{n_failed} failure{'s' if n_failed != 1 else ''}"
    return f"{items}, {fails}."


def oldest_uncompiled(settings: Settings, act: Activity, limit: int = OLDEST_UNCOMPILED) -> list[str]:
    path = settings.vault_path / COMPILE_QUEUE
    if not path.exists():
        return []
    out = []
    for m in _QUEUE_UNCHECKED.finditer(path.read_text(encoding="utf-8")):
        target, tags = m.group(1), m.group(2).strip()
        item = act.by_summary.get(target)
        title = _alias(item.title) if item and item.title else target.rsplit("/", 1)[-1]
        captured = local_day(item.captured_at) if item else None
        when = f"(captured {captured.isoformat()})" if captured else "(captured: unknown)"
        out.append(" ".join(p for p in (f"- [[{target}|{title}]]", tags, when) if p))
        if len(out) >= limit:
            break
    return out


def render_weekly(settings: Settings, act: Activity, monday: date) -> tuple[dict[str, Any], str]:
    sunday = monday + timedelta(days=6)
    week = iso_week(monday)
    done = sorted(act.in_range(act.done, monday, sunday), key=lambda i: (i.done_at or "", i.id))
    failed = sorted(act.in_range(act.failed, monday, sunday), key=lambda i: (i.updated_at, i.id))
    meta = {
        "type": "digest",
        "week": week,
        "start": monday,
        "end": sunday,
        "items": len(done),
        "failed": len(failed),
        "tags": ["digest", "weekly"],
    }
    lines = [
        f"# Week {week} ({monday.isoformat()} to {sunday.isoformat()})",
        "",
        _summary_line(len(done), len(failed)),
        "",
    ]
    if done:
        by_type = Counter(i.source_type for i in done)
        lines += ["## Counts", "", "| Source type | Items |", "|---|---|"]
        lines += [f"| {t} | {n} |" for t, n in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))]
        prim = Counter(primary(i) for i in done)
        second = Counter(d for i in done for d in i.domains[1:])
        lines += ["", "| Domain | Primary | Also tagged |", "|---|---|---|"]
        order = domain_order(settings, set(prim) | set(second))
        if prim.get(UNSORTED):
            order.append(UNSORTED)
        lines += [f"| {d} | {prim.get(d, 0)} | {second.get(d, 0)} |" for d in order]
        lines += ["", "## All TL;DRs by domain", "", *grouped_entries(settings, done, with_date=True)]
        unsorted = [i for i in done if primary(i) == UNSORTED]
        if unsorted:
            lines += [f"### {UNSORTED} ({len(unsorted)})", "", *unsorted_entries(unsorted), ""]
    if failed:
        lines += ["## Failures this week", "", *(failure_entry(i) for i in failed), ""]
    oldest = oldest_uncompiled(settings, act)
    lines += ["## Oldest uncompiled sources", ""]
    lines += [*oldest, ""] if oldest else ["The compile queue is empty.", ""]
    return meta, "\n".join(lines).rstrip("\n")


# Merging with an existing note ---------------------------------------------------------
def merge_note(existing: str | None, meta: dict[str, Any], block: str) -> str:
    """Put the generated block into the note, keeping anything the user wrote.

    - no file, or only whitespace / front matter: fresh note
    - markers present: replace only the marked region
    - user text without markers: generated block first, the text under `## My notes`
    """
    generated = f"{START}\n{block}\n{END}"
    if existing is None or not existing.strip():
        return fm.dump(meta) + "\n" + generated + "\n"
    try:
        old_meta, body = fm.split(existing)
    except yaml.YAMLError:
        old_meta, body = {}, existing  # broken YAML stays in the body, untouched, rather than lost
    head = _merged_front_matter(existing[: len(existing) - len(body)], old_meta, meta)
    start, end = body.find(START), body.find(END)
    if start != -1 and end > start:
        return head + body[:start] + generated + body[end + len(END) :]
    user = body.strip("\n")
    if not user.strip():
        return head + "\n" + generated + "\n"
    if not user.lstrip().startswith(MY_NOTES):
        user = f"{MY_NOTES}\n\n{user}"
    return head + "\n" + generated + "\n\n" + user + "\n"


def _merged_front_matter(old_head: str, old_meta: dict[str, Any], meta: dict[str, Any]) -> str:
    """Our keys over the user's, their extra keys kept. `old_head` is the original fence block."""
    try:
        return fm.dump({**old_meta, **meta})
    except TypeError:
        # The user's front matter has nested YAML our flat emitter cannot write back.
        # Keep theirs verbatim rather than lose it; the body is still regenerated.
        return old_head


# Orchestration -------------------------------------------------------------------------
def days_to_process(queue: Queue, act: Activity, today: date) -> list[date]:
    last = queue.get_state(STATE_KEY)
    floor = today - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
    if last:
        start = date.fromisoformat(last) + timedelta(days=1)
    else:
        earliest = act.earliest()
        start = max(earliest, floor) if earliest else today
    start = min(start, today)  # today is always (re)written; a future state (clock change) cannot skip it
    return [start + timedelta(days=n) for n in range((today - start).days + 1)]


def weeks_to_process(days: list[date], today: date) -> list[date]:
    """Mondays of completed ISO weeks touched by `days`, plus last week."""
    this_monday = monday_of(today)
    mondays = {monday_of(d) for d in days} | {this_monday - timedelta(days=7)}
    return sorted(m for m in mondays if m < this_monday)


def _write(vault: Vault, rel: str, meta: dict[str, Any], block: str) -> bool:
    existing = vault.read(rel) if vault.exists(rel) else None
    text = merge_note(existing, meta, block)
    if text == existing:
        return False
    vault.write(rel, text)
    return True


def _commit_message(days: list[date], weeks: list[str]) -> str:
    parts = []
    if days:
        parts.append(days[0].isoformat() if len(days) == 1 else f"{days[0].isoformat()}..{days[-1].isoformat()}")
    parts += weeks
    return "auto: digest " + " + ".join(parts)


def run(
    settings: Settings,
    queue: Queue,
    *,
    day: date | None = None,
    week: str | None = None,
    commit: bool = True,
    today: date | None = None,
) -> list[str]:
    """Write digest notes. Returns the vault-relative paths written (unchanged notes are skipped).

    - no `day`/`week`: catch up (see module docstring) and close yesterday
    - `day`: write that day only; state untouched
    - `week`: write that ISO week's rollup (e.g. "2026-W39"); with `day` too, both
    """
    if not settings.vault_path.is_dir():
        # Vault.write would silently create a bare folder with only digests in it.
        raise FileNotFoundError(f"vault {settings.vault_path} does not exist; run `kb init` first")
    today = today or datetime.now().astimezone().date()
    act = Activity.load(queue)
    tracker = Tracker(settings.vault_path, queue)
    vault = Vault(settings.vault_path, tracker)
    explicit = day is not None or week is not None

    days = [day] if day else ([] if explicit else days_to_process(queue, act, today))
    mondays = [parse_week(week)] if week else ([] if explicit else weeks_to_process(days, today))

    written: list[str] = []
    written_days: list[date] = []
    written_weeks: list[str] = []
    for d in days:
        if not act.active(d):
            continue
        rel = f"{DIGEST_DAILY}/{d.isoformat()}.md"
        meta, block = render_daily(settings, act, d)
        if _write(vault, rel, meta, block):
            written.append(rel)
            written_days.append(d)
    for monday in mondays:
        name = iso_week(monday)
        rel = f"{DIGEST_WEEKLY}/{name}.md"
        if not week and (vault.exists(rel) or not _week_active(act, monday)):
            continue  # auto mode: never rewrite a closed week, never write an empty one
        meta, block = render_weekly(settings, act, monday)
        if _write(vault, rel, meta, block):
            written.append(rel)
            written_weeks.append(name)

    if not explicit:
        yesterday = today - timedelta(days=1)
        last = queue.get_state(STATE_KEY)
        if not last or date.fromisoformat(last) < yesterday:
            queue.set_state(STATE_KEY, yesterday.isoformat())
    if commit and written:
        tracker.commit(message=_commit_message(written_days, written_weeks))
    return written


def _week_active(act: Activity, monday: date) -> bool:
    return any(act.active(monday + timedelta(days=n)) for n in range(7))
