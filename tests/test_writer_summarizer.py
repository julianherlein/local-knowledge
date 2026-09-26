from datetime import date

from kb_llm import FakeLLMClient

from kb import fm, writer
from kb.models import FetchedItem, SummaryResult, TagResult
from kb.summarizer import SCHEMA, build_prompt, render_body, summarize
from kb.textutil import head_tail


def test_head_tail_truncation():
    text = "A" * 1000 + "B" * 1000
    out, cut = head_tail(text, 100)
    assert cut and out.startswith("A") and out.endswith("B") and "omitted" in out
    assert len(out) <= 100 * 4 + 80
    assert head_tail("short", 100) == ("short", False)


def test_summary_prompt_carries_metadata_and_truncates():
    prompt, cut = build_prompt(
        {"source_type": "youtube", "title": "T", "channel": "C", "duration": "1:00:00"}, "x" * 400_000, 1000
    )
    assert cut and "Channel: C" in prompt and len(prompt) < 5000


def test_summarize_uses_schema(settings):
    fake = FakeLLMClient(
        lambda req: {
            "title": "T",
            "tldr": "t",
            "key_points": ["k"],
            "claims": [],
            "concepts": [],
            "entities": [],
            "open_questions": [],
        }
    )
    s, cut = summarize(settings, fake, {"title": "T", "source_type": "web"}, "body")
    assert s.title == "T" and not cut and fake.calls[0].json_schema == SCHEMA


def test_render_body_matches_sdd_sections():
    body = render_body(SummaryResult(title="T  x", tldr="a\nb", key_points=["k1", " "], concepts=["c"], entities=[]))
    assert body.splitlines()[0] == "# T x"
    assert "**TL;DR:** a b" in body
    for h in ["## Key points", "## Notable claims / numbers", "## Candidate concepts & entities", "## Open questions"]:
        assert h in body
    assert "- concepts: c\n- entities: none" in body
    assert "- k1\n\n" in body  # blank bullet dropped


def _item(queue):
    iid = queue.enqueue("https://example.com/a", "telegram", note="read later").item_id
    return queue.get(iid)


def test_raw_file_format(queue):
    item = _item(queue)
    fetched = FetchedItem(
        source_type="web",
        url="https://example.com/a",
        title="Why: we moved",
        body="# Body\n\ntext",
        author="Jane",
        published=date(2026, 9, 20),
        extra={"sitename": "Blog"},
    )
    text = writer.render_raw(item, fetched, TagResult(domains=["data-engineering"], method="classifier", language="en"))
    meta, body = fm.split(text)
    assert list(meta)[:11] == [
        "id",
        "title",
        "source_type",
        "url",
        "author",
        "published",
        "captured",
        "origin",
        "domains",
        "tag_method",
        "language",
    ]
    assert meta["published"] == date(2026, 9, 20)
    assert meta["tags"] == ["raw", "data-engineering"]
    assert meta["note"] == "read later" and meta["sitename"] == "Blog"
    assert "thread" not in meta
    assert body.strip() == "# Body\n\ntext"


def test_summary_file_links_raw(queue):
    meta = {
        "source_type": "web",
        "domains": ["tennis"],
        "published": date(2026, 1, 2),
        "captured": "2026-09-26T10:00:00-03:00",
        "url": "u",
    }
    s = SummaryResult(title="T", tldr="t", key_points=["k"])
    text = writer.render_summary("raw/web/2026-09-26-t.md", meta, 7, s, truncated=True)
    m, _ = fm.split(text)
    assert m["source"] == "[[raw/web/2026-09-26-t]]"
    assert m["status"] == "uncompiled" and m["type"] == "source-summary"
    assert m["tags"] == ["source", "tennis"] and m["truncated"] is True
    assert m["captured"] == date(2026, 9, 26)


def test_compile_queue_append_is_idempotent_and_retaggable():
    t = writer.append_compile_queue(None, "2026-09-26-a", ["system-design", "data-engineering"], "Line one.\nLine two.")
    assert t.endswith("- [ ] [[sources/2026-09-26-a]] #system-design #data-engineering — Line one. Line two.\n")
    assert writer.append_compile_queue(t, "2026-09-26-a", ["x"], "again") is None
    t2 = writer.append_compile_queue(t, "2026-09-26-a-2", ["tennis"], "other")
    assert t2.count("- [ ]") == 2
    re = writer.retag_compile_queue(t2, "2026-09-26-a", ["ai-llms"])
    assert "[[sources/2026-09-26-a]] #ai-llms — Line one." in re
    assert "[[sources/2026-09-26-a-2]] #tennis — other" in re
