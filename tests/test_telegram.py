"""Telegram capture adapter (SDD §8.2). All Bot API traffic is mocked with respx; no network."""

from __future__ import annotations

import copy
import json

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
    assert bot.replies == ["✓ queued (1)"]
    assert bot.sent[0]["chat_id"] == ME_ID
    assert bot.sent[0]["reply_parameters"]["message_id"] == 10
    first = bot.get_updates_params[0]
    assert first["timeout"] == 0 and first["limit"] == 100 and first["allowed_updates"] == ["message"]
    assert offset(ctx) == "101"
    assert ctx.queue.get_state(tg.LAST_POLL_KEY)


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
    assert bot.replies == ["✓ queued (2)"]


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
    assert bot.replies == ["✓ queued (1)"]
    assert offset(ctx) == "102"


def test_utf16_helpers():
    text = "a🔥b"
    assert tg._utf16_slice(text, 1, 2) == "🔥"
    assert tg._utf16_slice(text, 3, 1) == "b"
    assert tg._py_index(text, 3) == 2
    assert tg._py_index(text, 99) == 3


def test_duplicate_replies(ctx, router):
    ctx.queue.enqueue("https://blog.example.com/airflow", origin="cli")
    one_dup = m("multi")
    all_dup = m("plain_url")
    all_dup["message_id"] = 30
    ctx.queue.enqueue("https://example.com/post/1", origin="telegram")
    bot = Bot(router, [upd(100, one_dup), upd(101, all_dup)])
    rep = tg.poll(ctx)
    assert (rep.enqueued, rep.duplicates) == (1, 2)
    assert bot.replies == ["✓ queued (1), 1 already saved", "✓ already saved"]


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
            raise RuntimeError("disk I/O error")
        return real(url, **kw)

    monkeypatch.setattr(ctx.queue, "enqueue", flaky)
    second = m("text_link")
    Bot(router, [upd(100, m("plain_url")), upd(101, second), upd(102, m("photo_caption"))])
    rep = tg.poll(ctx)
    assert rep.errors == ["RuntimeError: disk I/O error"]
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
        (2, 0, "✓ queued (2)"),
        (1, 1, "✓ queued (1), 1 already saved"),
        (0, 3, "✓ already saved"),
        (0, 0, "✗ no URL found"),
    ],
)
def test_reply_text(q, d, text):
    assert tg.reply_text(q, d) == text


def test_command_parsing():
    assert tg.command({"text": "/start"}) == "start"
    assert tg.command({"text": "/Help@julian_kb_bot extra"}) == "help"
    assert tg.command({"text": "https://example.com"}) is None
    assert tg.command({"caption": "/start"}) is None
