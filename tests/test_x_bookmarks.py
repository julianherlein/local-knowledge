"""Bookmark poller: stop conditions, watermark safety, ordering, rate limits, auth retry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from test_x_support import API, authorize_queue, install_keyring, light_settings, load, x_settings

from kb.capture import CaptureContext, x_bookmarks
from kb.queue import Queue

BOOKMARKS = f"{API}/users/2244994945/bookmarks"
TOKEN = "https://api.x.com/2/oauth2/token"


@pytest.fixture
def settings():
    return light_settings()


@pytest.fixture
def queue():
    q = Queue(":memory:")
    yield q
    q.close()


@pytest.fixture
def keyring(monkeypatch):
    return install_keyring(monkeypatch)


@pytest.fixture
def ctx(settings, queue, http, keyring):
    s = x_settings(settings)
    authorize_queue(s, queue)
    return CaptureContext(s, http, queue)


def serve(pages: dict[str | None, tuple[list[str], str | None]], fail: dict | None = None):
    """A respx side effect serving bookmark pages keyed by pagination_token."""
    fail = fail or {}

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("pagination_token")
        if token in fail:
            return fail[token]
        ids, nxt = pages[token]
        body = {"data": [{"id": i, "edit_history_tweet_ids": [i], "text": f"post {i}"} for i in ids]}
        body["meta"] = {"result_count": len(ids), **({"next_token": nxt} if nxt else {})}
        return httpx.Response(200, json=body)

    return handler


def ids_in_queue(queue) -> list[str]:
    return [it.canonical_url.rsplit("/", 1)[-1] for it in queue.all_items()]


def enqueue(queue, *ids: str) -> None:
    for i in ids:
        queue.enqueue(f"https://x.com/i/status/{i}", "cli")


# Skips ---------------------------------------------------------------------------------
def test_skipped_when_disabled(settings, queue, http):
    rep = x_bookmarks.poll(CaptureContext(x_settings(settings, enabled=False), http, queue))
    assert rep.skipped and "disabled" in rep.message


def test_skipped_without_client_id(settings, queue, http):
    s = x_settings(settings).model_copy(update={"x_client_id": None})
    rep = x_bookmarks.poll(CaptureContext(s, http, queue))
    assert rep.skipped and "X_CLIENT_ID" in rep.message


def test_skipped_until_authorized(settings, queue, http, keyring):
    rep = x_bookmarks.poll(CaptureContext(x_settings(settings), http, queue))
    assert rep.skipped and "kb auth x" in rep.message


# Walks ---------------------------------------------------------------------------------
@respx.mock
def test_first_run_enqueues_only_the_first_page(ctx, queue):
    route = respx.get(BOOKMARKS).mock(return_value=httpx.Response(200, json=load("bookmarks_page.json")))
    rep = x_bookmarks.poll(ctx)
    page = [t["id"] for t in load("bookmarks_page.json")["data"]]
    assert route.call_count == 1
    assert ids_in_queue(queue) == list(reversed(page))  # oldest bookmark first
    assert all(it.origin == "x_bookmark" for it in queue.all_items())
    assert queue.get_state("x.last_bookmark_id") == page[0]
    assert rep.enqueued == 2 and not rep.errors
    assert "backfill-x" in rep.message
    # Bookmark reads are billed per post: no extra fields or expansions requested.
    assert dict(route.calls.last.request.url.params) == {"max_results": "20"}
    assert route.calls.last.request.headers["Authorization"] == "Bearer ACCESS-0"


@respx.mock
def test_stops_at_watermark_mid_page(ctx, queue):
    queue.set_state("x.last_bookmark_id", "8")
    respx.get(BOOKMARKS).mock(side_effect=serve({None: (["10", "9", "8", "7"], "t2")}))
    rep = x_bookmarks.poll(ctx)
    assert ids_in_queue(queue) == ["9", "10"]
    assert queue.get_state("x.last_bookmark_id") == "10"
    assert rep.enqueued == 2 and rep.seen == 4


@respx.mock
def test_watermark_compared_by_equality_not_order(ctx, queue):
    # Bookmark order is bookmark time: an old post bookmarked today sorts first.
    queue.set_state("x.last_bookmark_id", "500")
    respx.get(BOOKMARKS).mock(side_effect=serve({None: (["3", "900", "500", "1000"], None)}))
    x_bookmarks.poll(ctx)
    assert ids_in_queue(queue) == ["900", "3"]
    assert queue.get_state("x.last_bookmark_id") == "3"


@respx.mock
def test_watermark_on_page_two(ctx, queue):
    queue.set_state("x.last_bookmark_id", "4")
    route = respx.get(BOOKMARKS).mock(
        side_effect=serve({None: (["10", "9", "8", "7", "6"], "t2"), "t2": (["5", "4", "3"], "t3")})
    )
    x_bookmarks.poll(ctx)
    assert route.call_count == 2
    assert ids_in_queue(queue) == ["5", "6", "7", "8", "9", "10"]
    assert queue.get_state("x.last_bookmark_id") == "10"


@respx.mock
def test_known_page_newer_than_watermark_keeps_walking(ctx, queue):
    """X critic M4: posts already captured via Telegram/CLI must not hide older new bookmarks."""
    queue.set_state("x.last_bookmark_id", "1")
    enqueue(queue, "8", "7")
    route = respx.get(BOOKMARKS).mock(
        side_effect=serve({None: (["10", "9"], "t2"), "t2": (["8", "7"], "t3"), "t3": (["6", "5"], None)})
    )
    rep = x_bookmarks.poll(ctx)
    assert route.call_count == 3
    assert set(ids_in_queue(queue)) == {"10", "9", "8", "7", "6", "5"}
    assert queue.get_state("x.last_bookmark_id") == "10"
    assert rep.enqueued == 4 and rep.duplicates == 2


@respx.mock
def test_partially_known_page_keeps_walking(ctx, queue):
    queue.set_state("x.last_bookmark_id", "5")
    enqueue(queue, "9")  # captured earlier via Telegram, bookmarked later
    respx.get(BOOKMARKS).mock(side_effect=serve({None: (["10", "9"], "t2"), "t2": (["6", "5"], None)}))
    x_bookmarks.poll(ctx)
    assert ids_in_queue(queue) == ["9", "6", "10"]


@respx.mock
def test_rate_limit_mid_walk_keeps_watermark_then_recovers(ctx, queue):
    queue.set_state("x.last_bookmark_id", "3")
    reset = int((datetime.now(UTC) + timedelta(minutes=15)).timestamp())
    limited = httpx.Response(429, headers={"x-rate-limit-reset": str(reset)}, json={"title": "Too Many Requests"})
    pages = {None: (["10", "9"], "t2"), "t2": (["8", "7"], "t3"), "t3": (["6", "5", "4", "3"], None)}
    respx.get(BOOKMARKS).mock(side_effect=serve(pages, fail={"t2": limited}))

    rep = x_bookmarks.poll(ctx)
    assert len(rep.errors) == 1 and "rate limit" in rep.errors[0] and "resets at" in rep.errors[0]
    assert queue.get_state("x.last_bookmark_id") == "3"  # unchanged
    assert ids_in_queue(queue) == ["9", "10"]  # safe: enqueue dedups

    # Next run: page 1 is now fully known, but the walk must not stop there.
    respx.get(BOOKMARKS).mock(side_effect=serve(pages))
    rep2 = x_bookmarks.poll(ctx)
    assert not rep2.errors
    assert set(ids_in_queue(queue)) == {"4", "5", "6", "7", "8", "9", "10"}
    assert queue.get_state("x.last_bookmark_id") == "10"
    assert queue.get_state("x.walk_incomplete") is None


@respx.mock
def test_page_cap(ctx, queue):
    ctx.settings.x.max_bookmark_pages = 2
    queue.set_state("x.last_bookmark_id", "0")
    pages = {None: (["10", "9"], "a"), "a": (["8", "7"], "b"), "b": (["6", "5"], None)}
    route = respx.get(BOOKMARKS).mock(side_effect=serve(pages))
    rep = x_bookmarks.poll(ctx)
    assert route.call_count == 2
    assert ids_in_queue(queue) == ["7", "8", "9", "10"]
    assert "2-page cap" in rep.message


@respx.mock
def test_empty_bookmarks(ctx, queue):
    respx.get(BOOKMARKS).mock(return_value=httpx.Response(200, json={"meta": {"result_count": 0}}))
    rep = x_bookmarks.poll(ctx)
    assert rep.enqueued == 0 and not rep.errors
    assert queue.get_state("x.last_bookmark_id") is None


# Auth during the walk -------------------------------------------------------------------
@respx.mock
def test_401_triggers_one_refresh_then_succeeds(ctx, queue, keyring):
    queue.set_state("x.last_bookmark_id", "9")
    respx.post(TOKEN).mock(return_value=httpx.Response(200, json=load("token.json")))
    page = {"data": [{"id": "10", "text": "a"}, {"id": "9", "text": "b"}], "meta": {"result_count": 2}}
    route = respx.get(BOOKMARKS).mock(
        side_effect=[httpx.Response(401, json={"title": "Unauthorized"}), httpx.Response(200, json=page)]
    )
    rep = x_bookmarks.poll(ctx)
    assert not rep.errors
    assert route.calls[1].request.headers["Authorization"] == "Bearer ACCESS-1"
    assert keyring.store[("kb-engine", "x.refresh_token")] == "REFRESH-1"
    assert ids_in_queue(queue) == ["10"]


@respx.mock
def test_persistent_401_is_reported_not_raised(ctx, queue):
    respx.post(TOKEN).mock(return_value=httpx.Response(200, json=load("token.json")))
    route = respx.get(BOOKMARKS).mock(return_value=httpx.Response(401, json={"title": "Unauthorized"}))
    rep = x_bookmarks.poll(ctx)
    assert route.call_count == 2  # original + one retry, never a loop
    assert rep.errors and "kb auth x" in rep.errors[0]


@respx.mock
def test_dead_refresh_token_is_reported(ctx, queue):
    queue.set_state("x.access_expires_at", (datetime.now(UTC) - timedelta(minutes=5)).isoformat())
    respx.post(TOKEN).mock(return_value=httpx.Response(400, json={"error": "invalid_request"}))
    rep = x_bookmarks.poll(ctx)
    assert rep.errors and "kb auth x" in rep.errors[0]
    assert queue.get_state("x.last_bookmark_id") is None


@respx.mock
def test_expired_token_is_refreshed_once_for_the_whole_walk(ctx, queue):
    queue.set_state("x.last_bookmark_id", "1")
    queue.set_state("x.access_expires_at", (datetime.now(UTC) - timedelta(minutes=5)).isoformat())
    token_route = respx.post(TOKEN).mock(return_value=httpx.Response(200, json=load("token.json")))
    respx.get(BOOKMARKS).mock(side_effect=serve({None: (["3"], "p"), "p": (["2", "1"], None)}))
    x_bookmarks.poll(ctx)
    assert token_route.call_count == 1


@respx.mock
def test_missing_user_id_is_fetched_and_cached(ctx, queue):
    queue.set_state("x.user_id", None)
    me = respx.get(f"{API}/users/me").mock(return_value=httpx.Response(200, json=load("users_me.json")))
    respx.get(BOOKMARKS).mock(side_effect=serve({None: (["3"], None)}))
    x_bookmarks.poll(ctx)
    x_bookmarks.poll(ctx)
    assert me.call_count == 1
    assert queue.get_state("x.user_id") == "2244994945"


@respx.mock
def test_unbookmarked_watermark_does_not_import_history(ctx, queue):
    """X re-judge NEW-H2: the read-later pattern (un-bookmark the newest) must not walk into
    never-enqueued history and bill ~800 posts."""
    history = [str(i) for i in range(300, 0, -1)]  # newest first
    pages = {None: (history[:20], "p1")}
    for n, start in enumerate(range(20, 300, 20), 1):
        pages[f"p{n}"] = (history[start : start + 20], f"p{n + 1}" if start + 20 < 300 else None)
    respx.get(BOOKMARKS).mock(side_effect=serve(pages))
    x_bookmarks.poll(ctx)  # first run: page 1 only
    assert len(ids_in_queue(queue)) == 20 and queue.get_state("x.last_bookmark_id") == "300"

    # The user reads and un-bookmarks "300", then bookmarks something new.
    after = ["999"] + history[1:]
    pages2 = {None: (after[:20], "p1")}
    for n, start in enumerate(range(20, 300, 20), 1):
        pages2[f"p{n}"] = (after[start : start + 20], f"p{n + 1}" if start + 20 < 300 else None)
    route = respx.get(BOOKMARKS).mock(side_effect=serve(pages2))
    before = route.call_count  # same route object as the first run: counts accumulate
    rep = x_bookmarks.poll(ctx)
    assert route.call_count - before == 1
    assert rep.enqueued == 1 and "999" in ids_in_queue(queue)
    assert queue.get_state("x.last_bookmark_id") == "999"
