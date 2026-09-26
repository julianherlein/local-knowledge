"""Flat YAML front matter: a deterministic emitter and a body-preserving rewriter.

We emit YAML ourselves instead of via yaml.dump so the output is stable and
Obsidian-friendly: keys in the order given, ISO dates unquoted (typed as dates),
lists in flow style, strings quoted only when YAML would misread them. Parsing
uses yaml.safe_load. `replace_front_matter` never touches a byte of the body,
which is what keeps raw/ immutable when `kb tag` fixes domains.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

import yaml

_PLAIN_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 _./+@-]*$")
_RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "~", "y", "n"}
_LOOKS_NUMERIC = re.compile(r"^[-+]?(\d[\d_]*)?(\.\d+)?([eE][-+]?\d+)?$|^0x[0-9a-fA-F]+$|^\d{4}-\d{2}-\d{2}")


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
    if (
        s
        and _PLAIN_SAFE.match(s)
        and s == s.strip()
        and s.lower() not in _RESERVED
        and not _LOOKS_NUMERIC.match(s)
        and ": " not in s
        and " #" not in s
    ):
        return s
    return json.dumps(s, ensure_ascii=False)


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
