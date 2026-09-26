"""X post fetcher: long posts, t.co expansion, quotes, media, thread unrolling, errors."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
import respx
from test_x_support import API, NOW, authorize_queue, install_keyring, light_settings, load, x_settings

from kb.fetchers import FetchContext
from kb.fetchers import x as xf
from kb.models import FetchError
from kb.queue import Queue

SEARCH = f"{API}/tweets/search/recent"
TOKEN = "https://api.x.com/2/oauth2/token"
IDS = {
    "note": load("tweet_note.json")["data"]["id"],
    "quote": load("tweet_quote.json")["data"]["id"],
    "t1": load("thread_root.json")["data"]["id"],
    "t6": load("thread_mid.json")["data"]["id"],
    "reply": load("tweet_reply_other.json")["data"]["id"],
}
PAGE1, PAGE2 = load("thread_search_page1.json"), load("thread_search_page2.json")


@pytest.fixture
def settings():
    return light_settings()


@pytest.fixture
def queue():
    q = Queue(":memory:")
    yield q
    q.close()


@pytest.fixture
def ctx(settings, queue, http, monkeypatch):
    install_keyring(monkeypatch)
    s = x_settings(settings)
    authorize_queue(s, queue)
    return FetchContext(s, http, queue)


def item_for(queue, post_id: str, origin: str = "x_bookmark", inline_text: str | None = None):
    res = queue.enqueue(f"https://x.com/i/status/{post_id}", origin, inline_text=inline_text)
    return queue.get(res.item_id)


def lookup_route(post_id: str, fixture: str | None = None, **resp):
    route = respx.get(f"{API}/tweets/{post_id}")
    return route.mock(return_value=httpx.Response(200, json=load(fixture)) if fixture else httpx.Response(**resp))


def search_pages(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=PAGE2 if request.url.params.get("next_token") else PAGE1)


# Single posts --------------------------------------------------------------------------
@respx.mock
def test_long_post_uses_note_tweet_and_expands_links(ctx, queue):
    route = lookup_route(IDS["note"], "tweet_note.json")
    search = respx.get(SEARCH).mock(return_value=httpx.Response(200, json={"meta": {"result_count": 0}}))
    got = xf.fetch(item_for(queue, IDS["note"]), ctx, now=NOW)

    params = route.calls.last.request.url.params
    assert "note_tweet" in params["tweet.fields"] and "lang" in params["tweet.fields"]
    assert "attachments.media_keys" in params["expansions"]
    assert params["media.fields"] == "type,url,preview_image_url,alt_text"

    assert "backfills boring, which is the goal. Asset-based" in got.body  # the full note, not the truncated text
    assert "Airflow & onto Dagster" in got.body  # HTML entities decoded
    assert "Full write-up: https://blog.example.com/airflow-to-dagster?utm_source=x" in got.body
    assert "t.co" not in got.body
    assert "![Failure rate chart](https://pbs.twimg.com/media/GabcDEF.jpg)" in got.body
    assert got.body.startswith(
        f"**Jane Doe (@jdoe)** · 2026-09-25 · [post](https://x.com/jdoe/status/{IDS['note']})\n\n"
    )
    assert got.title == "@jdoe: Why we moved our pipelines off Airflow & onto Dagster, a thread in one post."
    assert got.author == "Jane Doe (@jdoe)"
    assert got.published == date(2026, 9, 25)
    assert got.language == "en"
    assert got.outlinks == ["https://blog.example.com/airflow-to-dagster?utm_source=x"]
    assert got.url == f"https://x.com/jdoe/status/{IDS['note']}"
    assert got.extra == {"x_id": IDS["note"], "author_handle": "jdoe", "thread_posts": 1}
    # A recent root is checked for a thread; nothing found -> a verified single post.
    assert search.calls.last.request.url.params["query"] == f"conversation_id:{IDS['note']} from:jdoe"
    assert got.thread == "complete"


@respx.mock
def test_quote_post_video_and_old_root(ctx, queue):
    lookup_route(IDS["quote"], "tweet_quote.json")
    search = respx.get(SEARCH).mock(return_value=httpx.Response(500))
    got = xf.fetch(item_for(queue, IDS["quote"]), ctx, now=NOW)
    oid = load("tweet_quote.json")["includes"]["tweets"][0]["id"]

    assert not search.called  # root older than 7 days: recent search cannot see it
    assert got.thread == "incomplete"
    assert "This is the best explanation of CDC I have read\n\n" in got.body
    assert f"[video](https://x.com/jdoe/status/{IDS['quote']})" in got.body
    assert (
        "> **@other:** Change data capture, explained:\n"
        "> log-based beats polling. Details https://other.dev/cdc\n"
        f"> [quoted post](https://x.com/other/status/{oid})"
    ) in got.body
    assert "t.co" not in got.body
    assert got.outlinks == []  # the quoted post's links belong to the quoted post


@respx.mock
def test_reply_to_someone_else_is_a_single_post(ctx, queue):
    lookup_route(IDS["reply"], "tweet_reply_other.json")
    search = respx.get(SEARCH).mock(return_value=httpx.Response(500))
    got = xf.fetch(item_for(queue, IDS["reply"]), ctx, now=NOW)
    assert got.thread is None and not search.called
    assert got.title == "@jdoe: @other agreed, this matches what we saw"


# Threads -------------------------------------------------------------------------------
def _assert_thread(got):
    sections = got.body.rstrip("\n").split("\n\n---\n\n")
    assert len(sections) == 4
    assert "How we cut warehouse costs by 60%" in sections[0]
    assert sections[1] == "1/ First, we stopped running full refreshes."
    assert (
        sections[2] == "2/ Second, incremental models everywhere: https://docs.getdbt.com/docs/build/incremental-models"
    )
    assert sections[3] == "4/ Finally, we turned off the dev warehouse at night."  # parent deleted, still kept
    assert "good question" not in got.body and "one more detail" not in got.body  # replies to others
    assert got.thread == "complete"
    assert got.extra["thread_posts"] == 4
    assert got.published == date(2026, 9, 24)
    assert got.outlinks == ["https://docs.getdbt.com/docs/build/incremental-models"]


@respx.mock
def test_thread_unrolled_from_root(ctx, queue):
    lookup_route(IDS["t1"], "thread_root.json")
    search = respx.get(SEARCH).mock(side_effect=search_pages)
    got = xf.fetch(item_for(queue, IDS["t1"]), ctx, now=NOW)
    _assert_thread(got)
    assert search.call_count == 2  # paginated
    first = search.calls[0].request.url.params
    assert first["query"] == f"conversation_id:{IDS['t1']} from:jdoe" and first["max_results"] == "100"
    assert search.calls[1].request.url.params["next_token"] == PAGE1["meta"]["next_token"]
    assert got.body.startswith(f"**Jane Doe (@jdoe)** · 2026-09-24 · [post](https://x.com/jdoe/status/{IDS['t1']})")
    assert got.title == "@jdoe: How we cut warehouse costs by 60%. A thread 🧵"


@respx.mock
def test_mid_thread_bookmark_fetches_the_root(ctx, queue):
    mid = lookup_route(IDS["t6"], "thread_mid.json")
    root = lookup_route(IDS["t1"], "thread_root.json")
    respx.get(SEARCH).mock(side_effect=search_pages)
    got = xf.fetch(item_for(queue, IDS["t6"]), ctx, now=NOW)
    assert mid.called and root.called
    _assert_thread(got)
    assert got.url == f"https://x.com/jdoe/status/{IDS['t6']}"
    assert got.extra["x_id"] == IDS["t6"]


@respx.mock
def test_thread_older_than_seven_days_is_incomplete(ctx, queue):
    lookup_route(IDS["t6"], "thread_mid.json")
    lookup_route(IDS["t1"], "thread_root.json")
    search = respx.get(SEARCH).mock(side_effect=search_pages)
    got = xf.fetch(item_for(queue, IDS["t6"]), ctx, now=NOW + timedelta(days=8))
    assert not search.called
    assert got.thread == "incomplete" and got.extra["thread_posts"] == 1
    assert "2/ Second, incremental models" in got.body


@respx.mock
def test_mid_thread_with_deleted_root_keeps_the_post(ctx, queue):
    lookup_route(IDS["t6"], "thread_mid.json")
    lookup_route(IDS["t1"], "tweet_not_found.json")
    got = xf.fetch(item_for(queue, IDS["t6"]), ctx, now=NOW)
    assert got.thread == "incomplete" and got.extra["thread_posts"] == 1


@respx.mock
def test_search_rate_limit_is_retried_then_degrades_on_last_attempt(ctx, queue):
    lookup_route(IDS["t1"], "thread_root.json")
    respx.get(SEARCH).mock(return_value=httpx.Response(429, headers={"x-rate-limit-reset": "1790000000"}))
    it = item_for(queue, IDS["t1"])
    with pytest.raises(FetchError) as exc:
        xf.fetch(it, ctx, now=NOW)
    assert not exc.value.permanent and exc.value.reason == "x_rate_limited"
    it.attempts = ctx.settings.run.max_attempts - 1
    got = xf.fetch(it, ctx, now=NOW)
    assert got.thread == "incomplete"


# Errors --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("fixture", "reason"), [("tweet_not_found.json", "x_not_found"), ("tweet_protected.json", "x_protected")]
)
@respx.mock
def test_errors_array_is_permanent(ctx, queue, fixture, reason):
    lookup_route("1234567890123456789", fixture)
    with pytest.raises(FetchError) as exc:
        xf.fetch(item_for(queue, "1234567890123456789"), ctx, now=NOW)
    assert exc.value.permanent and exc.value.reason == reason


@respx.mock
def test_401_after_refresh_is_permanent(ctx, queue):
    respx.post(TOKEN).mock(return_value=httpx.Response(200, json=load("token.json")))
    route = lookup_route("123456789012", status_code=401, json={"title": "Unauthorized"})
    with pytest.raises(FetchError) as exc:
        xf.fetch(item_for(queue, "123456789012"), ctx, now=NOW)
    assert route.call_count == 2
    assert exc.value.permanent and exc.value.reason == "http_401"


@pytest.mark.parametrize(
    ("status", "permanent", "reason"),
    [(429, False, "x_rate_limited"), (503, False, "x_error"), (404, True, "x_not_found"), (403, True, "http_403")],
)
@respx.mock
def test_http_status_classification(ctx, queue, status, permanent, reason):
    lookup_route("123456789012", status_code=status, json={"title": "err"})
    with pytest.raises(FetchError) as exc:
        xf.fetch(item_for(queue, "123456789012"), ctx, now=NOW)
    assert exc.value.permanent is permanent and exc.value.reason == reason


@respx.mock
def test_network_error_is_transient(ctx, queue):
    respx.get(f"{API}/tweets/123456789012").mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(FetchError) as exc:
        xf.fetch(item_for(queue, "123456789012"), ctx, now=NOW)
    assert not exc.value.permanent


def test_not_authorized_is_transient(settings, queue, http, monkeypatch):
    install_keyring(monkeypatch)
    ctx = FetchContext(x_settings(settings), http, queue)
    with pytest.raises(FetchError) as exc:
        xf.fetch(item_for(queue, "123456789012"), ctx, now=NOW)
    assert not exc.value.permanent and "kb auth x" in str(exc.value)


# Pure helpers --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("code", "iso"),
    [
        ("en", "en"),
        ("es", "es"),
        ("pt-BR", "pt"),
        ("in", "id"),
        ("iw", "he"),
        ("und", None),
        ("zxx", None),
        ("qme", None),
        ("qht", None),
        (None, None),
        ("ckb", None),
    ],
)
def test_iso_lang(code, iso):
    assert xf.iso_lang(code) == iso


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-24T10:00:00.000Z",
        "Thu Sep 24 10:00:00 +0000 2026",
        "2026-09-24 07:00:00 -03:00",
        "2026-09-24 10:00:00 +00:00",
    ],
)
def test_parse_created_formats(value):
    got = xf.parse_created(value)
    assert got is not None and got.timestamp() == NOW.replace(day=24, hour=10).timestamp()


def test_snowflake_time_matches_created_at():
    assert xf.snowflake_time(IDS["t1"]) == NOW.replace(day=24, hour=10)
    assert xf.snowflake_time("20") is None


def test_expand_text_drops_trailing_media_link_without_entity():
    r = xf.expand_text("look at this https://t.co/abc123", [], has_attachments=True)
    assert r.text == "look at this" and r.outlinks == []


def test_expand_text_keeps_an_explained_trailing_link_and_matches_whole_tokens():
    ents = [{"url": "https://t.co/abc", "expanded_url": "https://a.example/"}]
    r = xf.expand_text("see https://t.co/abcd and https://t.co/abc", ents, has_attachments=True)
    assert r.text == "see https://t.co/abcd and https://a.example/"
    assert r.outlinks == ["https://a.example/"]


def test_self_chain_ignores_other_authors_and_sorts():
    root = xf.Post(id="100", text="root", author_id="a")
    a = xf.Post(id="102", text="a2", author_id="a", replied_to="101", in_reply_to_user_id="a")
    b = xf.Post(id="101", text="a1", author_id="a", replied_to="100", in_reply_to_user_id="a")
    other = xf.Post(id="103", text="x", author_id="z", replied_to="102", in_reply_to_user_id="a")
    assert [p.id for p in xf.self_chain(root, [a, other, b])] == ["100", "101", "102"]
