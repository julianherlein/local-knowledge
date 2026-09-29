"""Telegram capture adapter (SDD §8.2). All Bot API traffic is mocked with respx; no network."""

from __future__ import annotations

import copy
import json
import sqlite3

import httpx
import pytest
import respx
from conftest import FIXTURES

from kb.capture import CaptureContext
from kb.capture import telegram as tg
from kb.config import Settings, TelegramSettings, load_settings
from kb.queue import Queue

TOKEN = "123456:AAF-secret-token-DO-NOT-LEAK"
BASE = f"https://api.telegram.org/bot{TOKEN}"
ME_ID = 123456789
MESSAGES = json.loads((FIXTURES / "telegram" / "messages.json").read_text(encoding="utf-8"))


def m(name: str) -> dict:
    return copy.deepcopy(MESSAGES[name])


def upd(uid: int, message: dict) -> dict:
    return {"update_id": uid, "message": message}


def ok(result) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def err(code: int, description: str, **params) -> httpx.Response:
    body = {"ok": False, "error_code": code, "description": description}
    if params:
        body["parameters"] = params
    return httpx.Response(code, json=body)


@pytest.fixture(scope="module")
def base_settings(tmp_path_factory) -> Settings:
    # The adapter touches neither the vault nor the disk DB: skip the shared git-vault
    # fixture (slow on Windows) and use an in-memory queue so the suite stays well under 1s.
    home = tmp_path_factory.mktemp("tg")
    return load_settings(home, vault_path=str(home / "vault"), x={"enabled": False})


@pytest.fixture(scope="module")
def http_client():
    # One client per module: building an SSL context per test costs ~100ms on Windows.
    with httpx.Client() as c:
        yield c


@pytest.fixture
def ctx(base_settings, http_client) -> CaptureContext:
    s = base_settings.model_copy(
        update={"telegram": TelegramSettings(enabled=True, allowed_chat_ids=[ME_ID]), "telegram_bot_token": TOKEN}
    )
    q = Queue(":memory:")
    yield CaptureContext(settings=s, http=http_client, queue=q)
    q.close()


def allow(ctx: CaptureContext, ids: list[int]) -> None:
    ctx.settings = ctx.settings.model_copy(update={"telegram": TelegramSettings(enabled=True, allowed_chat_ids=ids)})


class Bot:
    """A respx-backed fake Bot API: queued getUpdates batches, recorded calls."""

    def __init__(self, router: respx.MockRouter, *batches: list[dict]) -> None:
        self.batches = list(batches)
        self.get_updates_params: list[dict] = []
        self.sent: list[dict] = []
        self.get_updates = router.post(f"{BASE}/getUpdates").mock(side_effect=self._get_updates)
        self.send = router.post(f"{BASE}/sendMessage").mock(side_effect=self._send)
        self.get_me = router.post(f"{BASE}/getMe").mock(
            return_value=ok({"id": 1, "is_bot": True, "first_name": "KB", "username": "julian_kb_bot"})
        )

    def _get_updates(self, request: httpx.Request) -> httpx.Response:
        params = json.loads(request.content)
        self.get_updates_params.append(params)
        offset = params.get("offset")
        batch = self.batches.pop(0) if self.batches else []
        return ok([u for u in batch if offset is None or u["update_id"] >= offset])

    def _send(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.sent.append(body)
        return ok({"message_id": 1000 + len(self.sent), "chat": {"id": body["chat_id"]}, "date": 0, "text": ""})

    @property
    def replies(self) -> list[str]:
        return [s["text"] for s in self.sent]


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


def offset(ctx) -> str | None:
    return ctx.queue.get_state(tg.OFFSET_KEY)


# Skips and setup ---------------------------------------------------------------------------
def test_disabled_or_no_token_is_skipped(ctx, router):
    ctx.settings = ctx.settings.model_copy(update={"telegram": TelegramSettings(enabled=False)})
    rep = tg.poll(ctx)
    assert rep.skipped and "disabled" in rep.message
    ctx.settings = ctx.settings.model_copy(
        update={"telegram": TelegramSettings(enabled=True), "telegram_bot_token": None}
    )
    rep = tg.poll(ctx)
    assert rep.skipped and "TELEGRAM_BOT_TOKEN" in rep.message
    assert not router.calls


def test_empty_allowlist_peeks_without_consuming(ctx, router):
    allow(ctx, [])
    bot = Bot(router, [upd(500, m("plain_url"))], [upd(500, m("plain_url"))])
    rep = tg.poll(ctx)
    assert rep.skipped
    assert rep.message == (f"set telegram.allowed_chat_ids = [{ME_ID}] in config.toml (chats seen: {ME_ID} @julian)")
    assert "offset" not in bot.get_updates_params[0]
    assert offset(ctx) is None
    assert json.loads(ctx.queue.get_state(tg.SEEN_CHATS_KEY)) == {str(ME_ID): f"{ME_ID} @julian"}
    assert ctx.queue.all_items() == [] and bot.sent == []
    assert ctx.queue.get_state(tg.LAST_POLL_KEY) is None

    # Once configured, the same message is still there and gets captured.
    allow(ctx, [ME_ID])
    rep = tg.poll(ctx)
    assert rep.enqueued == 1 and offset(ctx) == "501"


def test_empty_allowlist_no_messages_yet(ctx, router):
    allow(ctx, [])
    Bot(router)
    rep = tg.poll(ctx)
    assert rep.skipped and "send your bot a message" in rep.message


# Draining ----------------------------------------------------------------------------------
def test_plain_url(ctx, router):
    bot = Bot(router, [upd(100, m("plain_url"))])
    rep = tg.poll(ctx)
    assert (rep.enqueued, rep.duplicates, rep.seen, rep.errors) == (1, 0, 1, [])
    [item] = ctx.queue.all_items()
    assert item.canonical_url == "https://example.com/post/1"
    assert item.origin == "telegram" and item.note is None and item.hint_tags == []
    assert bot.replies == ["✓ queued (#1 in queue)"]
    assert bot.sent[0]["chat_id"] == ME_ID
    assert bot.sent[0]["reply_parameters"]["message_id"] == 10
    first = bot.get_updates_params[0]
    assert first["timeout"] == tg.POLL_TIMEOUT_S and first["limit"] == 100 and first["allowed_updates"] == ["message"]
    assert offset(ctx) == "101"
    assert ctx.queue.get_state(tg.LAST_POLL_KEY)


class ColdBot(Bot):
    """A Bot API server that just woke up after the bot sat idle: the first call with
    `timeout=0` answers empty although messages are waiting; a long poll gets them.
    Seen live on 2026-09-29: `kb run` reported 0 new from Telegram, and the next run
    three minutes later picked up both messages.
    """

    warm = False

    def _get_updates(self, request: httpx.Request) -> httpx.Response:
        if not self.warm and json.loads(request.content).get("timeout", 0) == 0:
            self.get_updates_params.append(json.loads(request.content))
            return ok([])
        self.warm = True
        return super()._get_updates(request)


def test_cold_buffer_is_drained_on_the_first_run(ctx, router):
    bot = ColdBot(router, [upd(100, m("plain_url")), upd(101, m("multi"))])
    rep = tg.poll(ctx)
    assert (rep.enqueued, rep.errors) == (3, [])
    assert offset(ctx) == "102"
    assert bot.get_updates_params[0]["timeout"] == tg.POLL_TIMEOUT_S


def test_only_the_first_poll_of_a_run_waits(ctx, router):
    # A long poll on the final, empty call would add POLL_TIMEOUT_S to every run.
    bot = Bot(router, [upd(100, m("plain_url"))], [upd(101, m("multi"))])
    assert tg.poll(ctx).enqueued == 3
    assert [p["timeout"] for p in bot.get_updates_params] == [tg.POLL_TIMEOUT_S, 0, 0]


def test_cold_buffer_is_seen_by_setup_peek_and_doctor(ctx, router):
    allow(ctx, [])
    ColdBot(router, [upd(500, m("plain_url"))], [upd(500, m("plain_url"))])
    assert "chats seen" in tg.poll(ctx).message
    assert f"chat {ME_ID} @julian (not in allowed_chat_ids)" in tg.check(ctx)


def test_http_timeout_outlives_the_long_poll():
    # Otherwise an idle long poll surfaces as a network error on every run.
    assert tg.REQUEST_TIMEOUT_S >= tg.POLL_TIMEOUT_S + 10


def test_multiple_urls_hashtags_and_note(ctx, router):
    bot = Bot(router, [upd(100, m("multi"))])
    rep = tg.poll(ctx)
    assert rep.enqueued == 2
    items = ctx.queue.all_items()
    assert [i.canonical_url for i in items] == [
        "https://blog.example.com/airflow",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
    ]
    for i in items:
        assert i.hint_tags == ["de", "ai"]
        assert i.note == "Worth reading later and"
    assert bot.replies == ["✓ queued 2 (#1, #2 in queue)"]


def test_text_link_entity(ctx, router):
    Bot(router, [upd(100, m("text_link"))])
    tg.poll(ctx)
    [item] = ctx.queue.all_items()
    assert item.canonical_url == "https://x.com/i/status/1790000000000000001"
    assert item.source_type == "x"
    assert item.note == "Great thread on lakehouses"  # anchor text is the user's words, kept


def test_photo_caption(ctx, router):
    Bot(router, [upd(100, m("photo_caption"))])
    tg.poll(ctx)
    [item] = ctx.queue.all_items()
    assert item.canonical_url == "https://talks.example.org/iceberg"
    assert item.hint_tags == ["de"] and item.note == "Slide from the talk"


def test_emoji_before_url_uses_utf16_offsets(ctx, router):
    msg = m("emoji")
    ent = msg["entities"][0]
    # Sanity: slicing the Python str by the raw offsets is wrong for this fixture.
    assert msg["text"][ent["offset"] : ent["offset"] + ent["length"]] != "https://example.com/emoji-post"
    # Drop the regex fallback's chance to hide a slicing bug: a URL only Telegram detects.
    msg["text"] = msg["text"].replace("https://", "")
    msg["entities"][0]["length"] -= len("https://")
    msg["entities"][1]["offset"] -= len("https://")
    Bot(router, [upd(100, msg)])
    tg.poll(ctx)
    [item] = ctx.queue.all_items()
    assert item.canonical_url == "https://example.com/emoji-post"
    assert item.hint_tags == ["ai"]
    assert item.note == "🔥🔥 must read 👉 ok"


def test_album_replies_once(ctx, router):
    first = m("photo_caption")
    first["media_group_id"] = "13579"
    second = {k: v for k, v in first.items() if k not in ("caption", "caption_entities")}
    second["message_id"] = 20
    bot = Bot(router, [upd(100, first), upd(101, second)])
    rep = tg.poll(ctx)
    assert rep.enqueued == 1 and rep.seen == 2
    assert bot.replies == ["✓ queued (#1 in queue)"]
    assert offset(ctx) == "102"


def test_utf16_helpers():
    text = "a🔥b"
    assert tg._utf16_slice(text, 1, 2) == "🔥"
    assert tg._utf16_slice(text, 3, 1) == "b"
    assert tg._py_index(text, 3) == 2
    assert tg._py_index(text, 99) == 3


def test_duplicate_replies(ctx, router):
    done = ctx.queue.enqueue("https://blog.example.com/airflow", origin="cli").item_id
    ctx.queue.advance(done, "done")
    ctx.queue.enqueue("https://example.com/post/1", origin="telegram")  # sent before, not processed yet
    one_dup = m("multi")
    all_dup = m("plain_url")
    all_dup["message_id"] = 30
    bot = Bot(router, [upd(100, one_dup), upd(101, all_dup)])
    rep = tg.poll(ctx)
    assert (rep.enqueued, rep.duplicates) == (1, 2)
    # Item 2 is still waiting, so the new link is second in line.
    assert bot.replies == ["✓ queued (#2 in queue), 1 already saved", "✓ already queued (#1 in queue)"]


def test_forwarded_without_url_becomes_inline_item(ctx, router):
    Bot(router, [upd(100, m("forwarded_channel")), upd(101, m("forwarded_legacy_user"))])
    rep = tg.poll(ctx)
    assert rep.enqueued == 2
    a, b = ctx.queue.all_items()
    assert a.canonical_url == f"telegram://{ME_ID}/15" and a.source_type == "web"
    assert a.inline_text.startswith("Dagster vs Airflow")
    assert a.note == "forwarded from Data Eng Weekly"
    assert b.canonical_url == f"telegram://{ME_ID}/16"
    assert b.note == "forwarded from Ana Ruiz @anar"


def test_forward_source_variants():
    assert tg.forward_source({"forward_origin": {"type": "hidden_user", "sender_user_name": "Anon"}}) == (True, "Anon")
    assert tg.forward_source({"forward_origin": {"type": "chat", "sender_chat": {"title": "Group"}}}) == (
        True,
        "Group",
    )
    chan = {"type": "channel", "chat": {"title": "Chan"}, "author_signature": "Bob"}
    assert tg.forward_source({"forward_origin": chan}) == (True, "Chan (Bob)")
    assert tg.forward_source({"forward_sender_name": "Hidden", "forward_date": 1}) == (True, "Hidden")
    assert tg.forward_source({"text": "hi"}) == (False, None)


def test_plain_text_and_sticker_get_no_url_reply(ctx, router):
    bot = Bot(router, [upd(100, m("plain_text")), upd(101, m("sticker"))])
    rep = tg.poll(ctx)
    assert rep.enqueued == 0 and rep.seen == 2
    assert bot.replies == ["✗ no URL found", "✗ no URL found"]
    assert offset(ctx) == "102"


def test_start_and_help(ctx, router):
    help_msg = m("start")
    help_msg["text"], help_msg["message_id"] = "/help@julian_kb_bot", 40
    bot = Bot(router, [upd(100, m("start")), upd(101, help_msg)])
    tg.poll(ctx)
    assert len(bot.replies) == 2
    assert all("Send me a link" in r and "#ai" in r and "digest" in r for r in bot.replies)
    assert ctx.queue.all_items() == []


def test_foreign_chat_ignored_but_offset_advanced(ctx, router, caplog):
    bot = Bot(router, [upd(100, m("foreign")), upd(101, m("plain_url"))])
    with caplog.at_level("INFO", logger="kb.capture.telegram"):
        rep = tg.poll(ctx)
    assert rep.seen == 1 and rep.enqueued == 1
    assert [i.canonical_url for i in ctx.queue.all_items()] == ["https://example.com/post/1"]
    assert [s["chat_id"] for s in bot.sent] == [ME_ID]
    assert offset(ctx) == "102"
    assert "evil.example.com" not in caplog.text and "999" in caplog.text


def test_drains_two_batches(ctx, router):
    batch1 = [upd(100 + i, {**m("plain_url"), "message_id": i, "text": f"https://example.com/{i}"}) for i in range(3)]
    for u in batch1:
        u["message"].pop("entities")  # exercise the regex fallback too
    bot = Bot(router, batch1, [upd(103, m("multi"))])
    rep = tg.poll(ctx)
    assert rep.enqueued == 5 and rep.seen == 4
    assert [p.get("offset") for p in bot.get_updates_params] == [None, 103, 104]
    assert offset(ctx) == "104"


def test_resumes_from_stored_offset(ctx, router):
    ctx.queue.set_state(tg.OFFSET_KEY, "250")
    bot = Bot(router)
    tg.poll(ctx)
    assert bot.get_updates_params[0]["offset"] == 250


def test_crash_mid_batch_keeps_offset_of_last_handled_update(ctx, router, monkeypatch):
    real = ctx.queue.enqueue
    calls = {"n": 0}

    def flaky(url, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(url, **kw)

    monkeypatch.setattr(ctx.queue, "enqueue", flaky)
    second = m("text_link")
    Bot(router, [upd(100, m("plain_url")), upd(101, second), upd(102, m("photo_caption"))])
    rep = tg.poll(ctx)
    assert rep.errors == ["OperationalError: disk I/O error"]
    assert offset(ctx) == "101"  # update 100 done; 101 will be re-delivered next run
    assert rep.enqueued == 1
    assert ctx.queue.get_state(tg.LAST_POLL_KEY) is None

    # Next run: the re-delivered updates are captured and nothing is lost.
    monkeypatch.setattr(ctx.queue, "enqueue", real)
    Bot(router, [upd(101, second), upd(102, m("photo_caption"))])
    rep = tg.poll(ctx)
    assert rep.enqueued == 2 and offset(ctx) == "103"


def test_send_failure_does_not_block(ctx, router):
    bot = Bot(router, [upd(100, m("plain_url")), upd(101, m("multi"))])
    bot.send.mock(side_effect=[err(403, "Forbidden: bot was blocked by the user"), httpx.ConnectError("boom")])
    rep = tg.poll(ctx)
    assert rep.enqueued == 3 and rep.errors == []
    assert offset(ctx) == "102"


# Errors ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (err(401, "Unauthorized"), "bad TELEGRAM_BOT_TOKEN"),
        (err(409, "Conflict: can't use getUpdates method while webhook is active"), "deleteWebhook"),
        (err(429, "Too Many Requests: retry after 17", retry_after=17), "retry after 17s"),
        (httpx.ConnectTimeout(f"timed out calling {BASE}/getUpdates"), "network error"),
        (httpx.Response(502, text="<html>Bad Gateway</html>"), "HTTP 502"),
    ],
)
def test_errors_reported_without_raising_or_leaking_token(ctx, router, response, expected):
    kw = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
    router.post(f"{BASE}/getUpdates").mock(**kw)
    rep = tg.poll(ctx)
    assert not rep.skipped
    assert len(rep.errors) == 1 and expected in rep.errors[0]
    assert TOKEN not in rep.errors[0] and "AAF-secret" not in rep.errors[0]
    assert ctx.queue.get_state(tg.LAST_POLL_KEY) is None


def test_429_mid_drain_keeps_enqueued_items(ctx, router):
    router.post(f"{BASE}/sendMessage").mock(return_value=ok({}))
    router.post(f"{BASE}/getUpdates").mock(
        side_effect=[ok([upd(100, m("plain_url"))]), err(429, "Too Many Requests", retry_after=5)]
    )
    rep = tg.poll(ctx)
    assert rep.enqueued == 1 and "retry after 5s" in rep.errors[0]
    assert offset(ctx) == "101"


# check() -----------------------------------------------------------------------------------
def test_check_lists_bot_and_chats(ctx, router):
    allow(ctx, [ME_ID])
    ctx.queue.set_state(tg.OFFSET_KEY, "90")
    bot = Bot(router, [upd(100, m("plain_url")), upd(101, m("foreign"))])
    lines = tg.check(ctx)
    assert lines == [
        "bot @julian_kb_bot ok",
        f"chat {ME_ID} @julian (allowed)",
        "chat 999 Stranger (not in allowed_chat_ids)",
    ]
    assert bot.get_updates_params[0]["offset"] == 90  # confirms nothing new
    assert offset(ctx) == "90" and ctx.queue.all_items() == [] and bot.sent == []


def test_check_no_chats_and_errors(ctx, router):
    Bot(router)
    assert tg.check(ctx)[1].startswith("no chats seen yet")
    router.post(f"{BASE}/getMe").mock(return_value=err(401, "Unauthorized"))
    [line] = tg.check(ctx)
    assert "bad TELEGRAM_BOT_TOKEN" in line and TOKEN not in line
    router.post(f"{BASE}/getMe").mock(return_value=ok({"username": "julian_kb_bot"}))
    router.post(f"{BASE}/getUpdates").mock(return_value=err(409, "Conflict: terminated by other getUpdates request"))
    [line] = tg.check(ctx)
    assert "another getUpdates poller" in line


# Pure helpers ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("q", "d", "text"),
    [
        ([4, 5], 0, "✓ queued 2 (#4, #5 in queue)"),
        ([7], 1, "✓ queued (#7 in queue), 1 already saved"),
        ([], 3, "✓ already saved"),
        ([], 0, "✗ no URL found"),
    ],
)
def test_reply_text(q, d, text):
    assert tg.reply_text(q, d) == text


def test_reply_text_waiting():
    assert tg.reply_text([], 0, [2]) == "✓ already queued (#2 in queue)"
    assert tg.reply_text([5], 1, [2]) == "✓ queued (#5 in queue), already queued (#2 in queue), 1 already saved"


def test_command_parsing():
    assert tg.command({"text": "/start"}) == "start"
    assert tg.command({"text": "/Help@julian_kb_bot extra"}) == "help"
    assert tg.command({"text": "https://example.com"}) is None
    assert tg.command({"caption": "/start"}) is None


def _text_msg(mid: int, text: str, entities: list | None = None) -> dict:
    msg = {"message_id": mid, "date": 1790000000, "chat": {"id": ME_ID, "type": "private"}, "text": text}
    if entities is not None:
        msg["entities"] = entities
    return msg


def test_unparseable_url_skips_that_message_not_the_queue(ctx, router):
    """Critic H1: a URL urlsplit rejects must not stall every later message."""
    bad = _text_msg(1, "look https://[fe80::1 broken")
    good = _text_msg(2, "https://example.com/good")
    bot = Bot(router, [upd(100, bad), upd(101, good)])
    rep = tg.poll(ctx)
    assert offset(ctx) == "102"
    assert [i.canonical_url for i in ctx.queue.all_items()] == ["https://example.com/good"]
    assert rep.enqueued == 1
    texts = [b["text"] for b in bot.sent]
    assert texts[-1].startswith("✓ queued")


def test_parse_error_in_one_message_is_skipped_with_a_reply(ctx, router, monkeypatch):
    real = tg.parse_message

    def boom(msg):
        if msg["message_id"] == 1:
            raise KeyError("weird payload")
        return real(msg)

    monkeypatch.setattr(tg, "parse_message", boom)
    bot = Bot(router, [upd(100, _text_msg(1, "x")), upd(101, _text_msg(2, "https://example.com/ok"))])
    rep = tg.poll(ctx)
    assert offset(ctx) == "102" and rep.enqueued == 1
    assert any("skipped update 100" in e for e in rep.errors)
    assert bot.sent[0]["text"] == tg.UNREADABLE


def test_fragment_in_schemeless_url_entity_is_not_a_hashtag(ctx):
    msg = _text_msg(1, "example.com/#ai", [{"type": "url", "offset": 0, "length": 15}])
    assert tg.parse_message(msg).hashtags == []


def test_start_with_url_still_captures(ctx, router):
    msg = _text_msg(1, "/start https://example.com/s", [{"type": "bot_command", "offset": 0, "length": 6}])
    bot = Bot(router, [upd(100, msg)])
    rep = tg.poll(ctx)
    assert rep.enqueued == 1 and bot.sent[0]["text"] != tg.USAGE


def test_404_explains_a_malformed_token():
    """Re-judge L8: Telegram answers 404 for a token with stray spaces or quotes."""
    msg = tg.explain(tg.TelegramError(404, "Not Found"))
    assert "TELEGRAM_BOT_TOKEN" in msg and "spaces or quotes" in msg


# Queue position and done replies ----------------------------------------------------------
def finish(ctx, item_id: int, title: str, domains: list[str]) -> None:
    ctx.queue.advance(item_id, "done", title=title, domains=domains)


def tracked(ctx) -> dict[str, dict]:
    """{"chat:message": {item_id: last reported status}} from the adapter's state."""
    raw = json.loads(ctx.queue.get_state(tg.AWAITING_KEY) or "{}")
    return {k: v["items"] for k, v in raw.items()}


def test_queue_position_skips_finished_and_failed_items(ctx):
    q = ctx.queue
    a = q.enqueue("https://example.com/a", origin="cli").item_id
    b = q.enqueue("https://example.com/b", origin="cli").item_id
    c = q.enqueue("https://example.com/c", origin="cli").item_id
    q.advance(a, "done")
    q.fail(b, "boom", permanent=False, reason=None, max_attempts=3)  # retried after all others
    d = q.enqueue("https://example.com/d", origin="cli").item_id
    assert (q.position(c), q.position(d)) == (1, 2)


def test_done_reply_after_processing(ctx, router):
    bot = Bot(router, [upd(100, m("plain_url"))])
    tg.poll(ctx)
    [item] = ctx.queue.all_items()
    assert tg.notify_done(ctx) == 0  # still queued: nothing to say yet
    finish(ctx, item.id, "Post one", ["ai-llms"])
    assert tg.notify_done(ctx) == 1
    assert bot.replies[-1] == "✓ Done! Post one (ai-llms)"
    assert bot.sent[-1]["reply_parameters"]["message_id"] == 10 and bot.sent[-1]["chat_id"] == ME_ID
    assert "parse_mode" not in bot.sent[-1]  # titles are plain text, never markup
    assert tg.notify_done(ctx) == 0 and len(bot.sent) == 2  # sent once, then forgotten
    assert tracked(ctx) == {}


def test_multi_link_message_waits_for_every_item(ctx, router):
    bot = Bot(router, [upd(100, m("multi"))])
    tg.poll(ctx)
    first, second = ctx.queue.all_items()
    finish(ctx, first.id, "Airflow", ["data-engineering"])
    assert tg.notify_done(ctx) == 0
    ctx.queue.fail(second.id, "HTTP 404", permanent=True, reason="not_found", max_attempts=3)
    assert tg.notify_done(ctx) == 1
    assert bot.replies[-1] == (
        "✓ Done! Airflow (data-engineering)\n✗ Failed: https://youtube.com/watch?v=dQw4w9WgXcQ (not_found)"
    )


def test_retry_after_failed_reply_sends_done(ctx, router):
    bot = Bot(router, [upd(100, m("multi"))])
    tg.poll(ctx)
    first, second = ctx.queue.all_items()
    finish(ctx, first.id, "Airflow", [])
    ctx.queue.fail(second.id, "HTTP 503", permanent=True, reason="unavailable", max_attempts=3)
    assert tg.notify_done(ctx) == 1
    assert tracked(ctx) == {f"{ME_ID}:11": {str(second.id): "failed_permanent"}}  # kept for a retry
    assert tg.notify_done(ctx) == 0  # nothing changed, nothing re-sent
    ctx.queue.retry(second.id)
    assert tg.notify_done(ctx) == 0  # pending again
    finish(ctx, second.id, "Never gonna", ["music"])
    assert tg.notify_done(ctx) == 1
    assert bot.replies[-1] == "✓ Done! Never gonna (music)"  # only what changed
    assert tracked(ctx) == {}


def test_retryable_failure_waits_for_the_retry(ctx, router):
    Bot(router, [upd(100, m("plain_url"))])
    tg.poll(ctx)
    [item] = ctx.queue.all_items()
    ctx.queue.fail(item.id, "timeout", permanent=False, reason=None, max_attempts=3)
    assert tg.notify_done(ctx) == 0


def test_processed_duplicates_and_notes_are_not_tracked(ctx, router):
    done = ctx.queue.enqueue("https://example.com/post/1", origin="cli").item_id
    ctx.queue.advance(done, "done")
    note = m("plain_url")
    note["text"], note["entities"], note["message_id"] = "just a thought", [], 12
    Bot(router, [upd(100, m("plain_url")), upd(101, note)])
    tg.poll(ctx)
    assert tracked(ctx) == {}


def test_pending_duplicate_gets_done_on_both_messages(ctx, router):
    again = m("plain_url")
    again["message_id"] = 40
    bot = Bot(router, [upd(100, m("plain_url"))], [upd(101, again)])
    tg.poll(ctx)
    tg.poll(ctx)
    [item] = ctx.queue.all_items()
    assert bot.replies == ["✓ queued (#1 in queue)", "✓ already queued (#1 in queue)"]
    finish(ctx, item.id, "Post one", [])
    assert tg.notify_done(ctx) == 2
    assert sorted(s["reply_parameters"]["message_id"] for s in bot.sent[2:]) == [10, 40]


@pytest.mark.parametrize(("code", "kept"), [(429, True), (502, True), (403, False), (400, False)])
def test_done_reply_errors(ctx, router, code, kept):
    Bot(router, [upd(100, m("plain_url"))])
    tg.poll(ctx)
    [item] = ctx.queue.all_items()
    finish(ctx, item.id, "Post one", [])
    router.post(f"{BASE}/sendMessage").mock(return_value=err(code, "nope"))
    assert tg.notify_done(ctx) == 0
    assert bool(tracked(ctx)) is kept


def test_transient_error_stops_the_pass(ctx, router):
    # With the network down, one timeout per waiting message would hold the run lock for minutes.
    second = m("plain_url")
    second["message_id"], second["text"] = 41, "https://example.com/post/2"
    second["entities"] = [{"type": "url", "offset": 0, "length": len(second["text"])}]
    Bot(router, [upd(100, m("plain_url")), upd(101, second)])
    tg.poll(ctx)
    for it in ctx.queue.all_items():
        finish(ctx, it.id, "t", [])
    send = router.post(f"{BASE}/sendMessage").mock(side_effect=httpx.ConnectError("down"))
    before = send.call_count  # the route also counts the two capture replies
    assert tg.notify_done(ctx) == 0
    assert send.call_count - before == 1 and len(tracked(ctx)) == 2


def test_long_reply_is_capped_below_telegram_limit(ctx):
    class It:
        def __init__(self, i):
            self.id, self.title, self.canonical_url, self.domains = i, None, "https://e.com/" + "p" * 300, []
            self.status, self.error_reason, self.last_error = "failed_permanent", "not_found", None

    text = tg.done_text([It(i) for i in range(40)])
    assert len(text) <= tg.MAX_REPLY_CHARS
    assert text.splitlines()[-1].startswith("… and ") and text.splitlines()[-1].endswith(" more")
    assert tg.done_text([It(1)]).count("\n") == 0  # a short one is untouched


def test_stale_entries_expire(ctx, router):
    Bot(router, [upd(100, m("plain_url"))])
    tg.poll(ctx)
    raw = json.loads(ctx.queue.get_state(tg.AWAITING_KEY))
    for v in raw.values():
        v["since"] = "2020-01-01T00:00:00+00:00"
    ctx.queue.set_state(tg.AWAITING_KEY, json.dumps(raw))
    assert tg.notify_done(ctx) == 0 and tracked(ctx) == {}


def test_done_reply_skipped_when_telegram_off_and_never_raises(ctx, router):
    ctx.queue.set_state(tg.AWAITING_KEY, "not json")
    assert tg.notify_done(ctx) == 0  # unparseable state is ignored, not fatal
    ctx.settings = ctx.settings.model_copy(update={"telegram_bot_token": None})
    ctx.queue.set_state(tg.AWAITING_KEY, json.dumps({f"{ME_ID}:10": {"since": "x", "items": {"1": None}}}))
    assert tg.notify_done(ctx) == 0 and not router.calls


def test_removed_item_is_forgotten_silently(ctx, router):
    bot = Bot(router)
    ctx.queue.set_state(tg.AWAITING_KEY, json.dumps({f"{ME_ID}:10": {"since": tg.now_iso(), "items": {"999": None}}}))
    assert tg.notify_done(ctx) == 0 and bot.sent == []
    assert tracked(ctx) == {}


def test_failure_reason_is_one_short_line():
    class It:
        id, title, canonical_url, status, domains = 1, None, "https://e.com/x", "failed_permanent", []
        error_reason, last_error = None, "line one\n" + "x" * 500

    [line] = tg.done_text([It()]).splitlines()
    assert line.startswith("✗ Failed: https://e.com/x (line one xxx") and line.endswith("…)")
    assert len(line) < 260
