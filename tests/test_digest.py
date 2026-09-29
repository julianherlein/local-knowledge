"""`kb digest`: daily notes, catch-up, marker-preserving rewrites, weekly rollups, commits."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from conftest import git

import kb.queue as kbqueue
from kb import digest, fm, writer
from kb.config import load_settings
from kb.queue import Queue
from kb.vault import COMPILE_QUEUE

TODAY = date(2026, 9, 26)  # a Saturday in ISO week 2026-W39
DAILY = "digests/daily"


def ts(d: date, hour: int = 10, minute: int = 0) -> str:
    """ISO timestamp for a local wall-clock time, with this machine's offset (like now_iso)."""
    return datetime(d.year, d.month, d.day, hour, minute).astimezone().isoformat(timespec="seconds")


class Items:
    """Builds DB rows through the real Queue methods, at controlled timestamps."""

    def __init__(self, queue: Queue, monkeypatch) -> None:
        self.q = queue
        self.mp = monkeypatch
        self.n = 0

    def _at(self, when: str) -> None:
        self.mp.setattr(kbqueue, "now_iso", lambda: when)

    def done(
        self,
        day: date,
        title: str,
        domains: list[str],
        *,
        hour: int = 10,
        kind: str = "web",
        tldr: str | None = "A short summary.",
        summary: bool = True,
        captured: date | None = None,
    ) -> int:
        self.n += 1
        url = {
            "web": f"https://example.com/post-{self.n}",
            "youtube": f"https://www.youtube.com/watch?v=vid{self.n:08d}",
            "x": f"https://x.com/i/status/{1000 + self.n}",
        }[kind]
        self._at(ts(captured or day, hour))
        item_id = self.q.enqueue(url, "cli").item_id
        stem = f"{(captured or day).isoformat()}-post-{self.n}"
        self._at(ts(day, hour))
        self.q.update(
            item_id,
            domains=domains,
            title=title,
            tldr=tldr,
            summary_path=f"wiki/sources/{stem}.md" if summary else None,
        )
        self.q.advance(item_id, "done")
        return item_id

    def failed(self, day: date, *, permanent: bool, reason: str, error: str, hour: int = 11) -> int:
        self.n += 1
        self._at(ts(day, hour))
        item_id = self.q.enqueue(f"https://example.com/broken-{self.n}", "cli").item_id
        self.q.fail(item_id, error, permanent=permanent, reason=reason, max_attempts=3)
        return item_id


@pytest.fixture
def plain(tmp_path):
    """Settings with a vault that is not a git repo: no git subprocesses, fast."""
    s = load_settings(tmp_path / "home", vault_path=str(tmp_path / "vault"))
    s.vault_path.mkdir()
    return s


@pytest.fixture
def pq(plain):
    q = Queue(plain.db_path)
    yield q
    q.close()


@pytest.fixture
def items(pq, monkeypatch):
    return Items(pq, monkeypatch)


def read(settings, rel: str) -> str:
    return (settings.vault_path / rel).read_text(encoding="utf-8")


def write(settings, rel: str, text: str) -> None:
    p = settings.vault_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


# Daily note content --------------------------------------------------------------------
def test_daily_groups_by_primary_domain_with_secondary_tag(plain, pq, items):
    items.done(TODAY, "Why we moved off Airflow", ["data-engineering"], hour=9)
    items.done(TODAY, "Raft in practice", ["system-design", "data-engineering"], kind="youtube", hour=10)
    items.done(TODAY, "Serve & volley | a guide", ["tennis"], kind="x", hour=11, tldr="Come to  the net.\nOften.")
    items.done(date(2026, 9, 25), "Yesterday's item", ["software"])
    assert digest.run(plain, pq, day=TODAY, commit=False) == [f"{DAILY}/2026-09-26.md"]
    text = read(plain, f"{DAILY}/2026-09-26.md")
    meta, body = fm.split(text)
    assert meta == {"type": "digest", "date": TODAY, "items": 3, "tags": ["digest"]}
    assert "# Digest 2026-09-26" in body and "3 items ingested, 0 failures." in body
    assert "Yesterday's item" not in body
    # Sections follow config order: data-engineering, ai-llms, software, tennis, system-design.
    de = body.index("### [[wiki/domains/data-engineering|Data engineering]] (1)")
    tennis = body.index("### [[wiki/domains/tennis|Tennis]] (1)")
    sd = body.index("### [[wiki/domains/system-design|System design]] (1)")
    assert de < tennis < sd
    assert "- **[[wiki/sources/2026-09-26-post-1|Why we moved off Airflow]]** (web)\n  A short summary." in body
    assert "- **[[wiki/sources/2026-09-26-post-2|Raft in practice]]** (youtube) #data-engineering" in body
    assert "[[wiki/sources/2026-09-26-post-3|Serve & volley - a guide]]** (x)\n  Come to the net. Often." in body
    assert body.count(digest.START) == 1 and body.count(digest.END) == 1
    assert "—" not in text


def test_needs_attention_lists_failures_and_all_unsorted_items(plain, pq, items):
    items.done(TODAY, "Good one", ["software"])
    old_unsorted = items.done(date(2026, 9, 20), "Mystery post", ["unsorted"])
    new_unsorted = items.done(TODAY, "Another mystery", ["unsorted"])
    future_unsorted = items.done(date(2026, 9, 27), "Not yet", ["unsorted"])
    retry = items.failed(TODAY, permanent=False, reason="http_error", error="HTTP 503 from origin\nretry later")
    dead = items.failed(TODAY, permanent=True, reason="extraction_empty", error="only 120 characters extracted")
    items.failed(date(2026, 9, 24), permanent=True, reason="no_subtitles", error="old failure")
    digest.run(plain, pq, day=TODAY, commit=False)
    body = read(plain, f"{DAILY}/2026-09-26.md")
    attention = body[body.index("## Needs attention") :]
    assert f"- #{retry} https://example.com/broken-" in attention
    assert "`http_error`. HTTP 503 from origin retry later\n  Will be retried on the next `kb run`." in attention
    assert (
        f"`extraction_empty`. only 120 characters extracted\n  Gave up. After fixing the cause: `kb retry {dead}`"
        in attention
    )
    assert "old failure" not in body  # failures belong to the day they happened
    assert f"`kb tag {old_unsorted} <domain>`" in attention and f"`kb tag {new_unsorted} <domain>`" in attention
    assert f"kb tag {future_unsorted} " not in body  # a note never lists items from after its day
    assert "Another mystery" not in body[: body.index("## Needs attention")]  # unsorted only listed once
    assert "2 items ingested, 2 failures." in body


def test_compile_queue_counts_per_domain(plain, pq, items):
    text = None
    for stem, doms in [("a", ["tennis"]), ("b", ["tennis", "software"]), ("c", ["unsorted"]), ("d", ["software"])]:
        text = writer.append_compile_queue(text, stem, doms, "tl;dr")
    text = text.replace("- [ ] [[wiki/sources/d]]", "- [x] [[wiki/sources/d]]")
    write(plain, COMPILE_QUEUE, text)
    items.done(TODAY, "Anything", ["tennis"])
    digest.run(plain, pq, day=TODAY, commit=False)
    body = read(plain, f"{DAILY}/2026-09-26.md")
    assert "when this note was written: 3." in body
    assert "- #tennis: 2\n- #unsorted: 1 (tag them first)" in body


def test_item_without_summary_path_or_title_still_renders(plain, pq, items):
    item_id = items.done(TODAY, "", ["software"], summary=False, tldr=None)
    pq.update(item_id, title=None)
    digest.run(plain, pq, day=TODAY, commit=False)
    body = read(plain, f"{DAILY}/2026-09-26.md")
    assert "- **https://example.com/post-1** (web)" in body


def test_no_note_for_a_day_without_activity(plain, pq, items):
    items.done(date(2026, 9, 24), "Other day", ["software"])
    assert digest.run(plain, pq, day=TODAY, commit=False) == []
    assert not (plain.vault_path / DAILY / "2026-09-26.md").exists()


# Catch-up ------------------------------------------------------------------------------
def test_catch_up_writes_missed_days_closes_yesterday_and_keeps_today_open(plain, pq, items):
    items.done(date(2026, 9, 22), "Monday read", ["software"])
    items.done(date(2026, 9, 24), "Wednesday read", ["tennis"], hour=23)
    items.done(TODAY, "Saturday morning", ["ai-llms"], hour=8)
    written = digest.run(plain, pq, commit=False, today=TODAY)
    assert [p for p in written if p.startswith(DAILY)] == [
        f"{DAILY}/2026-09-22.md",
        f"{DAILY}/2026-09-24.md",
        f"{DAILY}/2026-09-26.md",
    ]
    assert pq.get_state(digest.STATE_KEY) == "2026-09-25"

    # Same day, later: a new item arrives; today's note is rewritten, closed days are not.
    items.done(TODAY, "Saturday evening", ["ai-llms"], hour=20)
    assert digest.run(plain, pq, commit=False, today=TODAY) == [f"{DAILY}/2026-09-26.md"]
    assert "Saturday evening" in read(plain, f"{DAILY}/2026-09-26.md")
    # Nothing new: nothing written (unchanged notes are skipped).
    assert digest.run(plain, pq, commit=False, today=TODAY) == []

    # Just after midnight: the 26th gets closed with everything up to 23:59.
    items.done(TODAY, "Late night", ["ai-llms"], hour=23)
    written = digest.run(plain, pq, commit=False, today=date(2026, 9, 27))
    assert written[0] == f"{DAILY}/2026-09-26.md"
    assert "Late night" in read(plain, f"{DAILY}/2026-09-26.md")
    assert pq.get_state(digest.STATE_KEY) == "2026-09-26"


def test_first_run_looks_back_at_most_14_days(plain, pq, items):
    items.done(date(2026, 8, 1), "Ancient", ["software"])
    items.done(date(2026, 9, 12), "Exactly 14 days ago", ["software"])
    written = digest.run(plain, pq, commit=False, today=TODAY)
    assert f"{DAILY}/2026-09-12.md" in written
    assert f"{DAILY}/2026-08-01.md" not in written


def test_catch_up_resumes_after_last_closed_day(plain, pq, items):
    items.done(date(2026, 9, 20), "Already digested", ["software"])
    items.done(date(2026, 9, 25), "New", ["software"])
    pq.set_state(digest.STATE_KEY, "2026-09-23")
    written = digest.run(plain, pq, commit=False, today=TODAY)
    assert f"{DAILY}/2026-09-25.md" in written and f"{DAILY}/2026-09-20.md" not in written


def test_explicit_date_does_not_move_state(plain, pq, items):
    items.done(date(2026, 9, 20), "Old", ["software"])
    pq.set_state(digest.STATE_KEY, "2026-09-10")
    assert digest.run(plain, pq, day=date(2026, 9, 20), commit=False, today=TODAY) == [f"{DAILY}/2026-09-20.md"]
    assert pq.get_state(digest.STATE_KEY) == "2026-09-10"


def test_future_state_after_a_clock_change_still_writes_today(plain, pq, items):
    items.done(TODAY, "Today", ["software"])
    pq.set_state(digest.STATE_KEY, "2026-10-30")
    assert f"{DAILY}/2026-09-26.md" in digest.run(plain, pq, commit=False, today=TODAY)
    assert pq.get_state(digest.STATE_KEY) == "2026-10-30"  # never moved backwards


# Existing notes ------------------------------------------------------------------------
def test_rewrite_keeps_user_text_around_the_markers(plain, pq, items):
    items.done(TODAY, "First", ["software"])
    digest.run(plain, pq, day=TODAY, commit=False)
    rel = f"{DAILY}/2026-09-26.md"
    meta, body = fm.split(read(plain, rel))
    user = fm.dump({**meta, "mood": "curious"}) + "Morning thoughts above.\n" + body + "\nEvening notes below.\n"
    write(plain, rel, user)
    items.done(TODAY, "Second", ["software"], hour=15)
    assert digest.run(plain, pq, day=TODAY, commit=False) == [rel]
    text = read(plain, rel)
    meta, body = fm.split(text)
    assert meta["mood"] == "curious" and meta["items"] == 2
    assert body.index("Morning thoughts above.") < body.index(digest.START) < body.index("Second")
    assert body.index(digest.END) < body.index("Evening notes below.")
    assert text.count(digest.START) == 1


@pytest.mark.parametrize("existing", ["", "\n\n  \n", "---\ntags: [daily]\n---\n\n"])
def test_empty_obsidian_daily_note_is_filled(plain, pq, items, existing):
    rel = f"{DAILY}/2026-09-26.md"
    write(plain, rel, existing)
    items.done(TODAY, "Filled in", ["software"])
    digest.run(plain, pq, day=TODAY, commit=False)
    text = read(plain, rel)
    assert text.startswith("---\n") and "Filled in" in text and digest.MY_NOTES not in text


def test_user_text_without_markers_moves_under_my_notes(plain, pq, items):
    rel = f"{DAILY}/2026-09-26.md"
    write(plain, rel, "Played doubles, serve felt off.\n")
    items.done(TODAY, "Serve mechanics", ["tennis"])
    digest.run(plain, pq, day=TODAY, commit=False)
    text = read(plain, rel)
    assert text.index(digest.END) < text.index("## My notes\n\nPlayed doubles, serve felt off.")
    items.done(TODAY, "More", ["tennis"], hour=16)  # the second rewrite uses the markers
    digest.run(plain, pq, day=TODAY, commit=False)
    assert read(plain, rel).count("## My notes") == 1 and "Played doubles" in read(plain, rel)


def test_broken_user_front_matter_is_kept_not_lost(plain, pq, items):
    rel = f"{DAILY}/2026-09-26.md"
    write(plain, rel, "---\nmood: [unclosed\n---\nmy text\n")
    items.done(TODAY, "Anything", ["software"])
    digest.run(plain, pq, day=TODAY, commit=False)
    text = read(plain, rel)
    assert "mood: [unclosed" in text and "my text" in text and "Anything" in text


# Weekly --------------------------------------------------------------------------------
def test_weekly_rollup_content(plain, pq, items):
    mon = date(2026, 9, 21)
    items.done(mon, "Kafka tiered storage", ["data-engineering", "system-design"], captured=date(2026, 9, 1))
    items.done(date(2026, 9, 23), "Topspin physics", ["tennis"], kind="youtube")
    items.done(date(2026, 9, 27), "Sunday LLM news", ["ai-llms"], kind="x")
    items.done(date(2026, 9, 28), "Next week", ["software"])
    items.done(date(2026, 9, 24), "Unclear", ["unsorted"])
    items.failed(date(2026, 9, 22), permanent=True, reason="no_subtitles", error="no captions")
    text = None
    for stem in ["2026-09-01-post-1", "ghost-not-in-db", "2026-09-23-post-2"]:
        text = writer.append_compile_queue(text, stem, ["tennis"], "x")
    write(plain, COMPILE_QUEUE, text)

    assert digest.run(plain, pq, week="2026-W39", commit=False, today=TODAY) == ["digests/weekly/2026-W39.md"]
    meta, body = fm.split(read(plain, "digests/weekly/2026-W39.md"))
    assert meta["week"] == "2026-W39" and meta["start"] == mon and meta["end"] == date(2026, 9, 27)
    assert meta["items"] == 4 and meta["failed"] == 1 and meta["tags"] == ["digest", "weekly"]
    assert "# Week 2026-W39 (2026-09-21 to 2026-09-27)" in body
    assert "| web | 2 |" in body and "| youtube | 1 |" in body and "| x | 1 |" in body
    assert "| data-engineering | 1 | 0 |" in body and "| system-design | 0 | 1 |" in body
    assert "| unsorted | 1 | 0 |" in body
    assert "Next week" not in body
    assert "**[[wiki/sources/2026-09-01-post-1|Kafka tiered storage]]** (web) #system-design 2026-09-21" in body
    assert "`kb tag 5 <domain>`" in body
    assert "## Failures this week" in body and "no captions" in body
    oldest = body[body.index("## Oldest uncompiled sources") :].splitlines()
    entries = [ln for ln in oldest if ln.startswith("- [[")]
    # Captured date comes from the DB (Sept 1), not from when the item finished (Sept 21).
    assert entries[0] == "- [[wiki/sources/2026-09-01-post-1|Kafka tiered storage]] #tennis (captured 2026-09-01)"
    assert entries[1] == "- [[wiki/sources/ghost-not-in-db|ghost-not-in-db]] #tennis (captured: unknown)"
    assert entries[2] == "- [[wiki/sources/2026-09-23-post-2|Topspin physics]] #tennis (captured 2026-09-23)"


def test_oldest_uncompiled_caps_at_ten_in_queue_order(plain, pq, items):
    text = None
    for n in range(15):
        text = writer.append_compile_queue(text, f"s{n:02d}", ["software"], "x")
    write(plain, COMPILE_QUEUE, text.replace("- [ ] [[wiki/sources/s00]]", "- [x] [[wiki/sources/s00]]"))
    items.done(date(2026, 9, 22), "Anything", ["software"])
    digest.run(plain, pq, week="2026-W39", commit=False, today=TODAY)
    body = read(plain, "digests/weekly/2026-W39.md")
    entries = [ln for ln in body.splitlines() if ln.startswith("- [[wiki/sources/s")]
    assert [e.split("|")[0] for e in entries] == [f"- [[wiki/sources/s{n:02d}" for n in range(1, 11)]


def test_auto_weekly_writes_last_completed_week_once(plain, pq, items):
    items.done(date(2026, 9, 17), "Week 38 item", ["software"])  # W38
    monday_w40 = date(2026, 9, 28)
    written = digest.run(plain, pq, commit=False, today=monday_w40)
    assert "digests/weekly/2026-W38.md" in written  # touched by the catch-up window
    assert "digests/weekly/2026-W39.md" not in written  # completed but empty
    assert "digests/weekly/2026-W40.md" not in written  # not completed
    items.done(date(2026, 9, 18), "Late addition", ["software"])
    again = digest.run(plain, pq, commit=False, today=monday_w40)
    assert not any(p.startswith("digests/weekly/") for p in again)  # a closed week is never rewritten


def test_missing_vault_is_an_error_not_a_new_folder(plain, pq, items):
    items.done(TODAY, "Anything", ["software"])
    plain.vault_path.rmdir()
    with pytest.raises(FileNotFoundError, match="kb init"):
        digest.run(plain, pq, commit=False, today=TODAY)
    assert not plain.vault_path.exists()


def test_bad_week_argument(plain, pq):
    with pytest.raises(ValueError):
        digest.run(plain, pq, week="2026-39", commit=False)
    with pytest.raises(ValueError):
        digest.run(plain, pq, week="2026-W60", commit=False)


# Git -----------------------------------------------------------------------------------
def test_commit_message_and_only_digest_paths(settings, queue, monkeypatch, vault_dir):
    it = Items(queue, monkeypatch)
    it.done(date(2026, 9, 17), "Last week", ["software"])
    it.done(date(2026, 9, 24), "Thursday", ["software"])
    it.done(TODAY, "Saturday", ["tennis"])
    (vault_dir / "README.md").write_text("user edit\n", encoding="utf-8")
    written = digest.run(settings, queue, today=TODAY)
    assert git(vault_dir, "log", "-1", "--format=%s").strip() == "auto: digest 2026-09-17..2026-09-26 + 2026-W38"
    committed = set(git(vault_dir, "show", "--name-only", "--format=", "HEAD").split())
    assert committed == set(written)
    assert " M README.md" in git(vault_dir, "status", "--porcelain")

    it.done(TODAY, "Saturday night", ["tennis"], hour=21)
    digest.run(settings, queue, day=TODAY)
    assert git(vault_dir, "log", "-1", "--format=%s").strip() == "auto: digest 2026-09-26"

    # The user writes in today's note in Obsidian (uncommitted). The next digest still rewrites
    # the note (keeping their text) but the Tracker leaves it out of the commit, so the message
    # must not claim it (critic finding M2). A note for another day is committed normally.
    note = vault_dir / "digests/daily/2026-09-26.md"
    note.write_text(note.read_text(encoding="utf-8") + "\nmy evening thoughts\n", encoding="utf-8")
    it.done(TODAY, "Even later", ["tennis"], hour=22)
    it.done(date(2026, 9, 25), "Friday", ["software"])
    written = digest.run(settings, queue, day=date(2026, 9, 25))
    assert git(vault_dir, "log", "-1", "--format=%s").strip() == "auto: digest 2026-09-25"
    digest.run(settings, queue, day=TODAY)  # only the user-edited note changes: no commit at all
    assert git(vault_dir, "log", "-1", "--format=%s").strip() == "auto: digest 2026-09-25"
    assert "my evening thoughts" in note.read_text(encoding="utf-8")
    assert " M digests/daily/2026-09-26.md" in git(vault_dir, "status", "--porcelain")
