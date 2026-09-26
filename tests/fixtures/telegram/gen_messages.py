"""Regenerate messages.json: `uv run python tests/fixtures/telegram/gen_messages.py`.

Entity offsets and lengths are UTF-16 code units, as the Bot API sends them. They are
computed here rather than typed by hand, so the emoji fixture is exact.
"""

from __future__ import annotations

import json
from pathlib import Path

D = 1790000000
ME = {"id": 123456789, "is_bot": False, "first_name": "Julian", "username": "julian", "language_code": "en"}
CHAT = {"id": 123456789, "first_name": "Julian", "username": "julian", "type": "private"}


def u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def ents(text: str, spans) -> list[dict]:
    out = []
    for kind, sub, *extra in spans:
        i = text.index(sub)
        e = {"offset": u16(text[:i]), "length": u16(sub), "type": kind}
        if extra:
            e["url"] = extra[0]
        out.append(e)
    return out


def msg(mid: int, text=None, spans=(), caption=None, cspans=(), **kw) -> dict:
    m = {"message_id": mid, "from": ME, "chat": CHAT, "date": D + mid}
    if text is not None:
        m["text"] = text
        if spans:
            m["entities"] = ents(text, spans)
    if caption is not None:
        m["photo"] = [
            {"file_id": "AgACAgQAAxkBAAIB", "file_unique_id": "AQADx", "file_size": 1433, "width": 90, "height": 67}
        ]
        m["caption"] = caption
        if cspans:
            m["caption_entities"] = ents(caption, cspans)
    m.update(kw)
    return m


def build() -> dict:
    m: dict[str, dict] = {}
    t = "https://example.com/post/1"
    m["plain_url"] = msg(10, t, [("url", t)])

    a, b = "https://blog.example.com/airflow?utm_source=x", "https://youtu.be/dQw4w9WgXcQ?si=abc"
    t = f"Worth reading later {a} and {b} #de #ai"
    m["multi"] = msg(11, t, [("url", a), ("url", b), ("hashtag", "#de"), ("hashtag", "#ai")])

    t = "Great thread on lakehouses"
    m["text_link"] = msg(12, t, [("text_link", "thread", "https://x.com/someone/status/1790000000000000001")])

    u = "https://talks.example.org/iceberg"
    m["photo_caption"] = msg(13, caption=f"Slide from the talk {u} #de", cspans=[("url", u), ("hashtag", "#de")])

    u = "https://example.com/emoji-post"
    t = f"🔥🔥 must read 👉 {u} ok #ai"
    m["emoji"] = msg(14, t, [("url", u), ("hashtag", "#ai")])

    channel = {"id": -1001234567890, "title": "Data Eng Weekly", "username": "deweekly", "type": "channel"}
    m["forwarded_channel"] = msg(
        15,
        "Dagster vs Airflow: we cut failures by 40% after moving. Full write-up soon.",
        forward_origin={"type": "channel", "chat": channel, "message_id": 881, "date": D - 3600},
    )

    m["forwarded_legacy_user"] = msg(
        16,
        "Serve and volley drills for doubles",
        forward_from={"id": 42, "is_bot": False, "first_name": "Ana", "last_name": "Ruiz", "username": "anar"},
        forward_date=D - 7200,
    )

    m["plain_text"] = msg(17, "remember to buy strings")
    m["start"] = msg(18, "/start", [("bot_command", "/start")])
    m["sticker"] = {
        "message_id": 19,
        "from": ME,
        "chat": CHAT,
        "date": D + 19,
        "sticker": {"file_id": "CAACAgIAAxkBAAIC", "file_unique_id": "AgADx", "type": "regular", "width": 512},
    }

    t = "https://evil.example.com/spam"
    m["foreign"] = {
        "message_id": 5,
        "from": {"id": 999, "is_bot": False, "first_name": "Stranger"},
        "chat": {"id": 999, "first_name": "Stranger", "type": "private"},
        "date": D,
        "text": t,
        "entities": ents(t, [("url", t)]),
    }
    return m


if __name__ == "__main__":
    out = Path(__file__).with_name("messages.json")
    out.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
