"""Deterministic text helpers.

Token counts are estimated without a tokenizer: ~4 characters per token for
Latin-script text, but ~1 token per character for CJK and other wide scripts, where
a flat 4 chars/token would underestimate by 4x and blow the context budget.
"""

from __future__ import annotations

import re

CHARS_PER_TOKEN = 4
CUT_MARKER = "\n\n[… {n} characters omitted from the middle of this source …]\n\n"
_WIDE_FROM = 0x2E80  # CJK radicals onward (CJK, kana, hangul, ...)


def _cost(ch: str) -> float:
    return 1.0 if ord(ch) >= _WIDE_FROM else 1.0 / CHARS_PER_TOKEN


def est_tokens(text: str) -> int:
    return int(sum(_cost(c) for c in text) + 0.999)


def _prefix_len(text: str, budget: float) -> int:
    """Number of leading characters whose estimated cost fits in `budget` tokens."""
    used = 0.0
    for i, ch in enumerate(text):
        used += _cost(ch)
        if used > budget:
            return i
    return len(text)


def head(text: str, max_tokens: int) -> str:
    n = _prefix_len(text, max_tokens)
    if n == len(text):
        return text
    cut = text.rfind(" ", 0, n)
    return text[: cut if cut > n * 0.8 else n] + " …"


def head_tail(text: str, max_tokens: int, head_share: float = 0.7) -> tuple[str, bool]:
    """Keep the start and the end of `text` within `max_tokens`. Returns (text, was_truncated)."""
    if est_tokens(text) <= max_tokens:
        return text, False
    budget = max(max_tokens - 20, 0)  # room for the marker
    h = _prefix_len(text, budget * head_share)
    t = _prefix_len(text[::-1], budget * (1 - head_share))
    omitted = len(text) - h - t
    return text[:h] + CUT_MARKER.format(n=omitted) + (text[-t:] if t else ""), True


# Inline hashtags ----------------------------------------------------------------------
# Obsidian turns `#word` anywhere in body text into a tag, and the hubs' Bases views and
# graph colors filter on tags. So a tweet saying "#tennis", or an LLM summary line
# mentioning "#ai", would file that page under the wrong domain. Escaped as `\#word`,
# which Obsidian renders as a plain "#word".
#
# A tag is `#` + [\w/-] characters with at least one non-digit (Obsidian's rule: `#1984`
# is not a tag, `#100DaysOfCode` is). Not a tag when preceded by a word char (C#,
# issue#4), '&' (&#39;), '/' or '=' (URL fragments), a quote (HTML attributes), '\'
# (already escaped) or '#', nor right after `](` (a markdown link to a heading).
_INLINE_TAG = re.compile(r"(?<![\w&/=\\#\"'])(?<!\]\()#(?=[\w/-]*[^\W\d])")
_INLINE_CODE = re.compile(r"(`+[^`\n]*?`+)")
_FENCE_OPEN = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def neutralize_hashtags(text: str) -> str:
    """Escape inline hashtags outside code. Headings (`# x`) are untouched.

    Line-based, following CommonMark's code rules: fenced blocks (any indent, 3+ backticks
    or tildes, closed only by the same character at least as long; an unclosed fence runs
    to the end), 4-space or tab indented lines, and inline code spans are left verbatim.
    """
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        m = _FENCE_OPEN.match(line)
        if fence is not None:
            if (
                m
                and m.group(1)[0] == fence[0]
                and len(m.group(1)) >= len(fence)
                and not line.strip()[len(m.group(1)) :]
            ):
                fence = None
            out.append(line)
            continue
        if m:
            fence = m.group(1)
            out.append(line)
            continue
        if line.startswith(("    ", "\t")):
            out.append(line)  # indented code (or a deep list continuation: leave it be)
            continue
        parts = _INLINE_CODE.split(line)
        out.append("".join(p if j % 2 else _INLINE_TAG.sub(r"\\#", p) for j, p in enumerate(parts)))
    return "\n".join(out)


def one_line(text: str, max_chars: int | None = None) -> str:
    s = " ".join((text or "").split())
    if max_chars and len(s) > max_chars:
        s = s[: max_chars - 1].rstrip() + "…"
    return s
