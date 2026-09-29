"""`kb backfill-x`: import of a twitter-web-exporter dump and rendering from the inline record."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx
from test_x_support import API, NOW, X_FIXTURES, authorize_queue, install_keyring, light_settings, load, x_settings

from kb.capture import CaptureContext, backfill
from kb.fetchers import FetchContext
from kb.fetchers import x as xf
from kb.models import FetchError
from kb.queue import Queue

EXPORT = X_FIXTURES / "export_bookmarks.json"
RECORDS = load("export_bookmarks.json")
WITH_META, _, PLAIN, _, NOTE = RECORDS


@pytest.fixture
def settings():
    return light_settings()


@pytest.fixture
def queue():
    q = Queue(":memory:")
    yield q
    q.close()


@pytest.fixture
def ctx(settings, queue, http):
    return CaptureContext(settings, http, queue)


def items(queue):
    return [it for it in queue.all_items()]


# Import --------------------------------------------------------------------------------
@respx.mock  # no route: the import must not touch the network
def test_import_real_exporter_shape_oldest_first(ctx, queue):
    rep = backfill.import_export(EXPORT, ctx)
    assert (rep.enqueued, rep.duplicates, rep.seen) == (3, 0, 3)
    assert rep.errors == ["record 2: no post id; skipped", "record 4: not an object; skipped"]
    got = items(queue)
    # created 2025-03, 2025-11, 2026-01: item ids ascend with post age.
    assert [it.canonical_url.rsplit("/", 1)[-1] for it in got] == [PLAIN["id"], NOTE["id"], WITH_META["id"]]
    assert all(it.origin == "backfill" and it.source_type == "x" for it in got)
    assert json.loads(got[0].inline_text) == PLAIN


def test_import_dedups_against_existing_items(ctx, queue):
    queue.enqueue(f"https://x.com/jdoe/status/{WITH_META['id']}", "x_bookmark")
    rep = backfill.import_export(EXPORT, ctx)
    assert (rep.enqueued, rep.duplicates) == (2, 1)
    rep2 = backfill.import_export(EXPORT, ctx)  # re-running the import is harmless
    assert (rep2.enqueued, rep2.duplicates) == (0, 3)


def test_import_accepts_a_wrapping_object_and_jsonl(ctx, queue, tmp_path):
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"data": [PLAIN, NOTE]}), encoding="utf-8")
    assert backfill.import_export(wrapped, ctx).enqueued == 2
    lines = tmp_path / "export.jsonl"
    lines.write_text(json.dumps(WITH_META) + "\n\n" + json.dumps({"id": 42}) + "\n", encoding="utf-8")
    rep = backfill.import_export(lines, ctx)
    assert rep.enqueued == 2 and not rep.errors


def test_import_accepts_numeric_ids_and_ids_from_urls(ctx, queue, tmp_path):
    f = tmp_path / "e.json"
    f.write_text(json.dumps([{"id": 1896202582225846272}, {"url": "https://twitter.com/a/status/1991412444165046272"}]))
    rep = backfill.import_export(f, ctx)
    assert rep.enqueued == 2 and not rep.errors


def test_import_bad_files_report_errors(ctx, tmp_path):
    missing = backfill.import_export(tmp_path / "nope.json", ctx)
    assert missing.errors and "cannot read" in missing.errors[0]
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    rep = backfill.import_export(garbage, ctx)
    assert rep.enqueued == 0 and "not JSON" in rep.errors[0]
    scalar = tmp_path / "scalar.json"
    scalar.write_text('"hello"', encoding="utf-8")
    assert "expected a JSON array" in backfill.import_export(scalar, ctx).errors[0]


def test_import_caps_error_lines(ctx, tmp_path):
    f = tmp_path / "bad.json"
    f.write_text(json.dumps([{"full_text": "no id"}] * 30))
    rep = backfill.import_export(f, ctx)
    assert len(rep.errors) == backfill.MAX_ERROR_LINES + 1
    assert rep.errors[-1] == "... and 10 more malformed records"


# Rendering from the inline record ---------------------------------------------------------
def _fetch(queue, http, settings, rec):
    res = queue.enqueue(f"https://x.com/i/status/{rec['id']}", "backfill", inline_text=json.dumps(rec))
    return xf.fetch(queue.get(res.item_id), FetchContext(settings, http, queue), now=NOW)


@respx.mock
def test_render_with_metadata_needs_no_network(queue, http, settings):
    got = _fetch(queue, http, settings, WITH_META)
    assert got.title == "@jdoe: Worth reading twice. Our notes"
    assert got.author == "Jane Doe (@jdoe)"
    assert got.published == date(2026, 1, 10)
    assert got.language == "en"
    assert got.thread == "incomplete"  # a root older than 7 days: cannot be verified
    assert got.outlinks == ["https://notes.example.org/evals"]
    assert "Worth reading twice. Our notes: https://notes.example.org/evals" in got.body
    assert (
        "> **@llmresearcher:** New paper: evals beat vibes. https://arxiv.org/abs/2601.00001\n"
        f"> [quoted post](https://x.com/llmresearcher/status/{WITH_META['quoted_status']})"
    ) in got.body
    assert "t.co" not in got.body
    assert got.extra == {"x_id": WITH_META["id"], "author_handle": "jdoe", "thread_posts": 1}


@respx.mock
def test_render_plain_record_resolves_tco_and_drops_media_link(queue, http, settings):
    tco = respx.head("https://t.co/TeNNiS0001").mock(
        return_value=httpx.Response(301, headers={"location": "https://tennis.example.com/serve-drills"})
    )
    got = _fetch(queue, http, settings, PLAIN)
    assert tco.calls.last.request.headers["User-Agent"] == "kb-engine"  # t.co 301s only for non-browsers
    assert got.body == (
        f"**Coach Sam (@coachsam)** · 2025-03-02 · [post](https://x.com/coachsam/status/{PLAIN['id']})\n\n"
        "Serve drills that actually work > 100 random serves. https://tennis.example.com/serve-drills\n\n"
        "![Serve toss diagram](https://pbs.twimg.com/media/Gserve.jpg?name=orig)\n"
    )
    assert got.title == "@coachsam: Serve drills that actually work > 100 random serves."
    assert got.outlinks == ["https://tennis.example.com/serve-drills"]
    assert got.language is None


@respx.mock
def test_unresolvable_tco_is_kept(queue, http, settings):
    respx.head("https://t.co/TeNNiS0001").mock(side_effect=httpx.ConnectError("offline"))
    got = _fetch(queue, http, settings, PLAIN)
    assert "https://t.co/TeNNiS0001" in got.body
    assert "MeDiA00001" not in got.body


@respx.mock
def test_render_note_record_keeps_paragraphs(queue, http, settings):
    got = _fetch(queue, http, settings, NOTE)
    assert "requirements first.\n\nPart two" in got.body
    assert got.published == date(2025, 11, 20)


@respx.mock
def test_quote_without_metadata_links_the_quoted_post(queue, http, settings):
    rec = {**PLAIN, "media": [], "full_text": "so true https://t.co/QqQqQ00001", "quoted_status": "2009535612318646272"}
    got = _fetch(queue, http, settings, rec)
    assert got.body.rstrip().endswith("> [quoted post](https://x.com/i/status/2009535612318646272)")
    assert "t.co" not in got.body


@respx.mock
def test_reply_to_someone_else_is_not_a_thread(queue, http, settings):
    meta = json.loads(json.dumps(WITH_META))
    meta["metadata"]["legacy"].update(in_reply_to_status_id_str="1", in_reply_to_user_id_str="42")
    meta["in_reply_to"] = "1"
    assert _fetch(queue, http, settings, meta).thread is None
    meta["metadata"]["legacy"]["in_reply_to_user_id_str"] = meta["metadata"]["legacy"]["user_id_str"]
    queue.conn.execute("DELETE FROM items")
    assert _fetch(queue, http, settings, meta).thread == "incomplete"  # a self-reply: part of a thread


@respx.mock
def test_backfill_fetch_via_api_uses_the_api(queue, http, settings, monkeypatch):
    install_keyring(monkeypatch)
    s = x_settings(settings, backfill_fetch_via_api=True)
    authorize_queue(s, queue)
    note = load("tweet_note.json")
    rec = {**PLAIN, "id": note["data"]["id"]}
    route = respx.get(f"{API}/tweets/{rec['id']}").mock(return_value=httpx.Response(200, json=note))
    respx.get(f"{API}/tweets/search/recent").mock(return_value=httpx.Response(200, json={"meta": {}}))
    got = _fetch(queue, http, s, rec)
    assert route.called and got.author == "Jane Doe (@jdoe)"


@respx.mock
def test_corrupt_inline_record_never_calls_the_api(queue, http, settings, monkeypatch):
    """X critic M3: a corrupt export record must not trigger a token refresh (dry runs!)."""
    install_keyring(monkeypatch)
    s = x_settings(settings)
    res = queue.enqueue("https://x.com/i/status/123456789012", "backfill", inline_text="{broken")
    route = respx.get(f"{API}/tweets/123456789012").mock(
        return_value=httpx.Response(200, json=load("tweet_not_found.json"))
    )
    authorize_queue(s, queue)
    with pytest.raises(FetchError) as exc:
        xf.fetch(queue.get(res.item_id), FetchContext(s, http, queue), now=NOW)
    assert (exc.value.permanent, exc.value.reason) == (True, "backfill_record_invalid")
    assert not route.called
