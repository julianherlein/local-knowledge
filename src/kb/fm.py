"""Flat YAML front matter: a deterministic emitter and a body-preserving rewriter.

We emit YAML ourselves instead of via yaml.dump so the output is stable and
Obsidian-friendly: keys in the order given, ISO dates unquoted (typed as dates),
lists in flow style, strings quoted only when YAML would misread them. Parsing
uses yaml.safe_load. `replace_front_matter` never touches a byte of the body,
which is what keeps raw/ immutable when `kb tag` fixes domains.

Scraped titles carry junk (cp1252 mojibake like \\x92, NEL, U+2028), and one bad
character in front matter would make every later step fail on that file. So a
string is written unquoted only if YAML reads it back as the identical string, and
quoted strings escape every character PyYAML refuses or treats as a line break.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

import yaml

_PLAIN_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9 _./+@-]*$")
# Outside PyYAML's printable set, plus characters it would read as line breaks or a BOM.
_UNPRINTABLE = re.compile(
    r"[^\t\n\r\x20-\x7e\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]|[\u2028\u2029\ufeff]"
)  # raw string: re itself decodes the escapes, so no formatter can turn them into literals
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _quote(s: str) -> str:
    s = _SURROGATE.sub(chr(0xFFFD), s)
    out = json.dumps(s, ensure_ascii=False)
    return _UNPRINTABLE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", out)


def _is_plain(s: str) -> bool:
    """Unquoted only if YAML reads it back as exactly the same string."""
    if not s or not _PLAIN_SAFE.match(s) or s != s.strip() or ": " in s or " #" in s:
        return False
    try:
        return yaml.safe_load(s) == s
    except yaml.YAMLError:
        return False


def _scalar(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    s = str(v)
    return s if _is_plain(s) else _quote(s)


def dump(meta: dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, (list, tuple)):
            lines.append(f"{k}: [{', '.join(_scalar(x) for x in v)}]")
        elif isinstance(v, dict):
            raise TypeError(f"front matter must be flat; {k!r} is a mapping")
        else:
            rendered = _scalar(v)
            lines.append(f"{k}: {rendered}" if rendered else f"{k}:")
    lines.append("---")
    return "\n".join(lines) + "\n"


_FM = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def split(text: str) -> tuple[dict[str, Any], str]:
    """Return (meta, body). The body is returned byte-for-byte as it appears after the closing fence."""
    m = _FM.match(text)
    if not m:
        return {}, text
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        return {}, text
    return meta, text[m.end() :]


def replace_front_matter(text: str, updates: dict[str, Any]) -> str:
    """Update keys in the front matter, keeping key order and the exact body bytes."""
    meta, body = split(text)
    for k, v in updates.items():
        meta[k] = v
    return dump(meta) + body


def render(meta: dict[str, Any], body: str) -> str:
    return dump(meta) + "\n" + body.rstrip("\n") + "\n"
