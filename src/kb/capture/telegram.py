"""Capture adapter: Telegram bot (SDD §8.2, D2).

The user sends links to a private bot; every `kb run` drains `getUpdates` with a short
poll (no webhook, no public endpoint). Telegram keeps unconfirmed updates for ~24h and
an update is confirmed as soon as `getUpdates` is called with an offset above its id,
so the offset is the only thing that can lose messages. Two rules follow:

* The offset (`telegram.offset` = last handled `update_id + 1`, SDD §6.2) is persisted
  right after each update is enqueued, never before. A crash re-delivers at most one
  update, and queue dedup absorbs it.
* Until `telegram.allowed_chat_ids` is configured, the adapter only peeks (it never
  sends a higher offset), so the first message sent while setting up is not lost.

Raw httpx calls, no bot framework. The token lives in the request URL, so every error
string is scrubbed before it reaches a log line or a report.

Message entity offsets are UTF-16 code units. Python indexes code points, so slicing
by entity offsets directly is wrong as soon as the text holds an emoji or other astral
character; `_utf16_slice` and `_py_index` do the conversion.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..normalize import TELEGRAM_SCHEME, URL_RE, InvalidURL, extract_hashtags, extract_urls, normalize
from ..queue import now_iso
from . import CaptureContext, CaptureReport

log = logging.getLogger("kb.capture.telegram")

API = "https://api.telegram.org"
SOURCE = "telegram"
OFFSET_KEY = "telegram.offset"
LAST_POLL_KEY = "telegram.last_poll_at"
SEEN_CHATS_KEY = "telegram.seen_chats"

BATCH_LIMIT = 100  # Bot API maximum for getUpdates
MAX_BATCHES = 50  # safety cap per run (5000 updates); the next run continues
REQUEST_TIMEOUT_S = 20.0

USAGE = (
    "Send me a link and I will save it to your knowledge base.\n"
    "Several links in one message are fine. Add hashtags to pick a domain, e.g. #ai #de.\n"
    "Any other text is kept as a note. Results arrive in the daily digest."
)
NO_URL = "✗ no URL found"
UNREADABLE = "✗ could not read this message; it was skipped"

_HASHTAG_RE = re.compile(r"(?<![\w#])#[A-Za-z][\w-]*")
_WS = re.compile(r"\s+")


# Bot API transport -------------------------------------------------------------------
class TelegramError(Exception):
    """A failed Bot API call. `code` is the Telegram error_code (None for network errors)."""

    def __init__(self, code: int | None, description: str, retry_after: int | None = None) -> None:
        super().__init__(description)
        self.code = code
        self.description = description
        self.retry_after = retry_after


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "<token>") if token else text


def _call(ctx: CaptureContext, method: str, **params: Any) -> Any:
    token = ctx.settings.telegram_bot_token or ""
    url = f"{API}/bot{token}/{method}"
    try:
        r = ctx.http.post(url, json=params, timeout=REQUEST_TIMEOUT_S)
    except httpx.HTTPError as e:
        raise TelegramError(None, _scrub(f"{type(e).__name__}: {e}", token)) from None
    try:
        data = r.json()
    except ValueError:
        raise TelegramError(r.status_code, f"HTTP {r.status_code} with a non-JSON body") from None
    if not isinstance(data, dict) or not data.get("ok"):
        data = data if isinstance(data, dict) else {}
        params_ = data.get("parameters") or {}
        raise TelegramError(
            data.get("error_code") or r.status_code,
            _scrub(str(data.get("description") or f"HTTP {r.status_code}"), token),
            params_.get("retry_after"),
        )
    return data.get("result")


def explain(err: TelegramError) -> str:
    """One human-readable line for a failed call. Never contains the token."""
    if err.code == 401:
        return "bad TELEGRAM_BOT_TOKEN (401 Unauthorized): copy the token from @BotFather into ~/.kb/.env"
    if err.code == 409:
        return (
            "409 Conflict: a webhook is set for this bot (call deleteWebhook) "
            f"or another getUpdates poller is using the same token ({err.description})"
        )
    if err.code == 429:
        wait = f"retry after {err.retry_after}s" if err.retry_after is not None else "retry later"
        return f"rate limited by Telegram (429), {wait}"
    if err.code is None:
        return f"network error talking to Telegram: {err.description}"
    return f"Telegram API error {err.code}: {err.description}"


def _get_updates(ctx: CaptureContext, offset: int | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": 0, "limit": BATCH_LIMIT, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    result = _call(ctx, "getUpdates", **params)
    return [u for u in (result or []) if isinstance(u, dict) and isinstance(u.get("update_id"), int)]


def _reply(ctx: CaptureContext, msg: dict[str, Any], text: str) -> None:
    """Best effort: a failed reply is logged and never blocks the offset or the run."""
    try:
        _call(
            ctx,
            "sendMessage",
            chat_id=msg["chat"]["id"],
            text=text,
            reply_parameters={"message_id": msg["message_id"], "allow_sending_without_reply": True},
        )
    except TelegramError as e:
        log.warning("telegram reply failed: %s", explain(e))


# Message parsing (pure) ----------------------------------------------------------------
def _utf16_slice(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[2 * offset : 2 * (offset + length)].decode("utf-16-le", errors="ignore")


def _py_index(text: str, u16_offset: int) -> int:
    """Code-point index of a UTF-16 offset (clamped to the text length)."""
    units = 0
    for i, ch in enumerate(text):
        if units >= u16_offset:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


@dataclass
class Parsed:
    urls: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    note: str | None = None
    text: str = ""


def _canonical(url: str) -> str | None:
    try:
        return normalize(url).canonical_url
    except InvalidURL:
        return None


def parse_message(msg: dict[str, Any]) -> Parsed:
    """URLs (deduped by canonical form), hashtags, and the leftover free text of a message."""
    text = msg.get("text")
    entities = msg.get("entities")
    if text is None:
        text = msg.get("caption")
        entities = msg.get("caption_entities")
    text = text or ""
    entities = [e for e in (entities or []) if isinstance(e, dict)]

    candidates: list[str] = []
    tags: list[str] = []
    cut: list[tuple[int, int]] = []  # code-point spans removed from the note
    for e in entities:
        kind, off, ln = e.get("type"), int(e.get("offset", 0)), int(e.get("length", 0))
        if kind == "url":
            candidates.append(_utf16_slice(text, off, ln))
        elif kind == "text_link" and e.get("url"):
            candidates.append(str(e["url"]))
        elif kind == "hashtag":
            tags.append(_utf16_slice(text, off, ln).lstrip("#").lower())
        else:
            continue
        if kind in ("url", "hashtag"):
            cut.append((_py_index(text, off), _py_index(text, off + ln)))
    candidates.extend(extract_urls(text))
    # Regex fallback for hashtags only outside entity spans, so "example.com/#ai" (a url
    # entity without a scheme) cannot become a domain override.
    outside = text
    for start, end in sorted(cut, reverse=True):
        outside = outside[:start] + " " + outside[end:]
    tags.extend(extract_hashtags(outside))

    urls: list[str] = []
    seen: set[str] = set()
    for u in candidates:
        key = _canonical(u)
        if key and key not in seen:
            seen.add(key)
            urls.append(u)
    hashtags = list(dict.fromkeys(t for t in tags if t))

    rest = text
    for start, end in sorted(cut, reverse=True):
        rest = rest[:start] + " " + rest[end:]
    rest = _HASHTAG_RE.sub(" ", URL_RE.sub(" ", rest))
    note = _WS.sub(" ", rest).strip() or None
    return Parsed(urls=urls, hashtags=hashtags, note=note, text=text)


def _person(user: dict[str, Any] | None) -> str | None:
    if not user:
        return None
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    handle = f"@{user['username']}" if user.get("username") else ""
    return " ".join(p for p in (name, handle) if p) or None


def forward_source(msg: dict[str, Any]) -> tuple[bool, str | None]:
    """(is_forwarded, human name of the origin). Handles `forward_origin` and legacy fields."""
    origin = msg.get("forward_origin")
    if isinstance(origin, dict):
        kind = origin.get("type")
        if kind == "user":
            return True, _person(origin.get("sender_user"))
        if kind == "hidden_user":
            return True, origin.get("sender_user_name")
        if kind == "chat":
            return True, (origin.get("sender_chat") or {}).get("title")
        if kind == "channel":
            title = (origin.get("chat") or {}).get("title")
            sig = origin.get("author_signature")
            return True, f"{title} ({sig})" if title and sig else title
        return True, None
    legacy = ("forward_from", "forward_from_chat", "forward_sender_name", "forward_date")
    if any(k in msg for k in legacy):
        name = (
            (msg.get("forward_from_chat") or {}).get("title")
            or _person(msg.get("forward_from"))
            or msg.get("forward_sender_name")
        )
        return True, name
    return False, None


def command(msg: dict[str, Any]) -> str | None:
    """`start` for "/start" or "/start@my_bot payload"; None when the text is not a command."""
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return None
    return text[1:].split(maxsplit=1)[0].split("@", 1)[0].lower() or None


def chat_label(chat: dict[str, Any]) -> str:
    label = f"@{chat['username']}" if chat.get("username") else chat.get("title") or _person(chat) or ""
    return f"{chat.get('id')} {label}".strip()


def reply_text(queued: int, duplicates: int) -> str:
    if queued and duplicates:
        return f"✓ queued ({queued}), {duplicates} already saved"
    if queued:
        return f"✓ queued ({queued})"
    if duplicates:
        return "✓ already saved"
    return NO_URL


# Seen chats (for setup) ------------------------------------------------------------------
def _seen_chats(ctx: CaptureContext, updates: list[dict[str, Any]]) -> dict[str, str]:
    """Merge chats in `updates` into state `telegram.seen_chats` ({id: label}) and return it."""
    try:
        seen = json.loads(ctx.queue.get_state(SEEN_CHATS_KEY) or "{}")
        if not isinstance(seen, dict):
            seen = {}
    except ValueError:
        seen = {}
    for u in updates:
        chat = (u.get("message") or {}).get("chat")
        if isinstance(chat, dict) and "id" in chat:
            seen[str(chat["id"])] = chat_label(chat)
    ctx.queue.set_state(SEEN_CHATS_KEY, json.dumps(seen, ensure_ascii=False))
    return seen


def _stored_offset(ctx: CaptureContext) -> int | None:
    raw = ctx.queue.get_state(OFFSET_KEY)
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        log.warning("ignoring unparseable %s=%r", OFFSET_KEY, raw)
        return None


# Public API ------------------------------------------------------------------------------
def poll(ctx: CaptureContext) -> CaptureReport:
    report = CaptureReport(source=SOURCE)
    s = ctx.settings
    if not s.telegram.enabled:
        report.skipped, report.message = True, "disabled in config"
        return report
    if not s.telegram_bot_token:
        report.skipped, report.message = True, "TELEGRAM_BOT_TOKEN not set in ~/.kb/.env"
        return report
    allowed = set(s.telegram.allowed_chat_ids)
    try:
        if not allowed:
            return _peek_for_setup(ctx, report)
        _drain(ctx, allowed, report)
    except TelegramError as e:
        report.errors.append(explain(e))
        log.warning("telegram poll stopped: %s", explain(e))
    except Exception as e:  # never raise out of a capture adapter; the offset is already safe
        msg = _scrub(f"{type(e).__name__}: {e}", s.telegram_bot_token)
        report.errors.append(msg)
        log.error("telegram poll failed: %s", msg)
    else:
        ctx.queue.set_state(LAST_POLL_KEY, now_iso())
    return report


def _peek_for_setup(ctx: CaptureContext, report: CaptureReport) -> CaptureReport:
    # The stored offset (or none) confirms nothing that was not already handled.
    updates = _get_updates(ctx, _stored_offset(ctx))
    seen = _seen_chats(ctx, updates)
    report.skipped = True
    if seen:
        ids = ", ".join(seen)
        chats = ", ".join(seen.values())
        report.message = f"set telegram.allowed_chat_ids = [{ids}] in config.toml (chats seen: {chats})"
    else:
        report.message = "telegram.allowed_chat_ids is empty: send your bot a message, then run `kb doctor`"
    return report


def _drain(ctx: CaptureContext, allowed: set[int], report: CaptureReport) -> None:
    offset = _stored_offset(ctx)
    for _ in range(MAX_BATCHES):
        updates = _get_updates(ctx, offset)
        if not updates:
            return
        progressed = False
        for u in sorted(updates, key=lambda x: x["update_id"]):
            uid = u["update_id"]
            if offset is not None and uid < offset:
                continue  # already handled; defensive against a server replay
            try:
                reply = _handle(ctx, u, allowed, report)
            except (sqlite3.Error, OSError):
                raise  # our side is broken: stop here, the update is retried next run
            except Exception as e:
                # Something in this one message is unparseable. Skipping it (with a reply)
                # beats stalling every later message until Telegram drops them at 24h.
                msg = _scrub(f"{type(e).__name__}: {e}", ctx.settings.telegram_bot_token or "")
                log.error("telegram: skipping unreadable update %s: %s", uid, msg)
                report.errors.append(f"skipped update {uid}: {msg}")
                m = u.get("message")
                reply = (m, UNREADABLE) if isinstance(m, dict) and m.get("chat") else None
            offset = uid + 1
            ctx.queue.set_state(OFFSET_KEY, str(offset))  # after enqueue, never before
            progressed = True
            if reply:
                _safe_reply(ctx, *reply)  # after the offset: a crash never re-sends a reply
        if not progressed:
            return
    log.info("telegram: stopped after %d batches, the next run continues", MAX_BATCHES)


def _safe_reply(ctx: CaptureContext, msg: dict[str, Any], text: str) -> None:
    try:
        _reply(ctx, msg, text)
    except Exception as e:  # replies are best effort, whatever goes wrong
        log.warning(
            "telegram: reply failed: %s", _scrub(f"{type(e).__name__}: {e}", ctx.settings.telegram_bot_token or "")
        )


def _handle(
    ctx: CaptureContext, update: dict[str, Any], allowed: set[int], report: CaptureReport
) -> tuple[dict[str, Any], str] | None:
    """Enqueue what one update carries. Returns the (message, reply text) to send, if any."""
    msg = update.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("chat"), dict):
        return None
    chat_id = msg["chat"].get("id")
    if chat_id not in allowed:
        log.info("telegram: ignoring update %s from chat %s (not in allowed_chat_ids)", update["update_id"], chat_id)
        return None
    report.seen += 1

    parsed = parse_message(msg)
    if command(msg) in ("start", "help") and not parsed.urls:
        return msg, USAGE

    queued = dups = 0
    if parsed.urls:
        for url in parsed.urls:
            res = ctx.queue.enqueue(url, origin=SOURCE, hint_tags=parsed.hashtags, note=parsed.note)
            if res.status == "queued":
                queued += 1
            elif res.status == "duplicate":
                dups += 1
    else:
        forwarded, source = forward_source(msg)
        if forwarded and parsed.text.strip():
            res = ctx.queue.enqueue(
                f"{TELEGRAM_SCHEME}{chat_id}/{msg.get('message_id')}",
                origin=SOURCE,
                hint_tags=parsed.hashtags,
                note=f"forwarded from {source}" if source else "forwarded message",
                inline_text=parsed.text.strip(),
            )
            queued, dups = int(res.status == "queued"), int(res.status == "duplicate")
    report.enqueued += queued
    report.duplicates += dups
    log.info("telegram: update %s -> %d queued, %d duplicate", update["update_id"], queued, dups)
    if not parsed.text and msg.get("media_group_id") and not (queued or dups):
        return None  # the other photos of an album: only the captioned one gets a reply
    return msg, reply_text(queued, dups)


def check(ctx: CaptureContext) -> list[str]:
    """Lines for `kb doctor`: bot identity plus the chats that have messaged it."""
    if not ctx.settings.telegram_bot_token:
        return ["TELEGRAM_BOT_TOKEN not set in ~/.kb/.env (create a bot with @BotFather)"]
    allowed = set(ctx.settings.telegram.allowed_chat_ids)
    try:
        me = _call(ctx, "getMe") or {}
        lines = [f"bot @{me.get('username', '?')} ok"]
        updates = _get_updates(ctx, _stored_offset(ctx))
    except TelegramError as e:
        return [explain(e)]
    except (httpx.InvalidURL, ValueError):
        return ["TELEGRAM_BOT_TOKEN looks malformed (stray whitespace or quotes in ~/.kb/.env?)"]
    seen = _seen_chats(ctx, updates)
    if not seen:
        lines.append("no chats seen yet: send your bot a message, then run `kb doctor` again")
    for cid, label in seen.items():
        mark = "allowed" if _as_int(cid) in allowed else "not in allowed_chat_ids"
        lines.append(f"chat {label} ({mark})")
    return lines


def _as_int(s: str) -> int | None:
    try:
        return int(s)
    except ValueError:
        return None
