"""WebVTT caption cleaning for YouTube transcripts (SDD §7.2).

YouTube serves two shapes of VTT and the same code has to handle both:

- Auto captions "roll": each cue shows the line already on screen plus a new line whose
  words carry inline timing tags (`hello<00:00:01.360><c> world</c>`), and 10ms cues
  freeze the finished line. Read naively, every phrase appears twice.
- Manual captions are plain cues with no repetition. Two identical consecutive cues are
  real speech ("No." / "No.") and must be kept.

A file is treated as rolling when most of its real (non-freeze) cues carry inline timing
tags; one karaoke-style cue in a manual file does not flip it. In rolling mode a cue's
first line is dropped when it repeats the last emitted line, which is exactly the
"line still on screen". A cue that starts with a blank line (screen cleared) repeats
nothing, so its words are always kept, even when they equal the previous line.

The parser is our own, line based and tolerant, instead of webvtt-py 0.5.1: that library
rejects a whole file for one malformed cue or a UTF-8 BOM, and splits cues on
whitespace-only lines, which YouTube uses inside cues. A cue we cannot read is skipped;
the rest of the transcript survives.

The result is merged into paragraphs with a `[mm:ss]` marker roughly every
`marker_every_s` seconds (`[h:mm:ss]` once the video reaches an hour).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

_TS = r"(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}"
_TIMING = re.compile(rf"^\s*({_TS})\s+-->\s+({_TS})")
_INLINE_TS = re.compile(r"<(?:\d+:)?\d{2}:\d{2}\.\d{3}>")
_TAG = re.compile(r"<[^>]*>")
_SENTENCE_END = re.compile(r"[.!?…][\"')\]]*$")

HOUR = 3600
FREEZE_S = 0.05
"""Cues shorter than this are YouTube's 'freeze the finished line' cues."""
ROLLING_SHARE = 0.5
PUNCTUATED_SHARE = 0.1
"""Below this share of sentence-ending segments the track is unpunctuated auto captions."""
HARD_CUT_FACTOR = 1.5


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    lines: list[str]
    """Raw payload lines, tags and whitespace-only lines included."""


@dataclass(frozen=True)
class Segment:
    start: float
    text: str


def format_ts(seconds: float, hours: bool = False) -> str:
    """`mm:ss`, or `h:mm:ss` when `hours` (videos of an hour or more use it everywhere)."""
    s = max(int(seconds), 0)
    h, rem = divmod(s, HOUR)
    m, sec = divmod(rem, 60)
    if hours:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m + h * 60:02d}:{sec:02d}"


def parse_ts(ts: str) -> float | None:
    try:
        total = 0.0
        for part in ts.replace(",", ".").split(":"):
            total = total * 60 + float(part)
        return total
    except ValueError:
        return None


def parse_cues(vtt_text: str) -> list[Cue]:
    """Every readable cue, in file order. Header, NOTE, STYLE and REGION blocks are ignored
    because only lines after a timing line are collected."""
    lines = vtt_text.lstrip("﻿").splitlines()
    cues: list[Cue] = []
    i, n = 0, len(lines)
    while i < n:
        m = _TIMING.match(lines[i])
        i += 1
        if not m:
            continue
        start, end = parse_ts(m.group(1)), parse_ts(m.group(2))
        payload: list[str] = []
        while i < n and lines[i] != "" and not _TIMING.match(lines[i]):
            line = lines[i]
            nxt = lines[i + 1] if i + 1 < n else ""
            # A whitespace-only or id-looking line right before the next timing line is a
            # separator / cue identifier, not caption text.
            if _TIMING.match(nxt) and (not line.strip() or " " not in line.strip()):
                i += 1
                break
            if not line.strip() and i + 2 < n and _TIMING.match(lines[i + 2]) and " " not in lines[i + 1].strip():
                i += 1
                break
            payload.append(line)
            i += 1
        if start is not None and end is not None:
            cues.append(Cue(start, end, payload))
    return cues


def _clean_line(line: str) -> str:
    return " ".join(html.unescape(_TAG.sub("", line)).split())


def is_rolling(cues: list[Cue]) -> bool:
    """Most cues carry inline word timings or are 10ms freeze cues: YouTube auto captions."""
    spoken = [c for c in cues if any(ln.strip() for ln in c.lines)]
    if not spoken:
        return False
    evidence = sum(1 for c in spoken if c.end - c.start < FREEZE_S or any(_INLINE_TS.search(ln) for ln in c.lines))
    return evidence / len(spoken) > ROLLING_SHARE


def parse_segments(vtt_text: str) -> tuple[list[Segment], float]:
    """Cue text as de-duplicated segments, plus the end time of the last cue."""
    cues = parse_cues(vtt_text)
    rolling = is_rolling(cues)
    segments: list[Segment] = []
    last_emitted: str | None = None
    last_end = 0.0
    for cue in cues:
        last_end = max(last_end, cue.end)
        screen_cleared = bool(cue.lines) and not cue.lines[0].strip()
        lines = [cl for cl in (_clean_line(raw) for raw in cue.lines) if cl]
        if rolling and lines and not screen_cleared and lines[0] == last_emitted:
            lines = lines[1:]
        if not lines:
            continue
        last_emitted = lines[-1]
        segments.append(Segment(cue.start, " ".join(lines)))
    return segments, last_end


def paragraphs(segments: list[Segment], marker_every_s: int) -> list[tuple[float, str]]:
    """Group segments into (start, text) paragraphs of roughly `marker_every_s` seconds.

    Punctuated tracks close a paragraph at the first sentence end after `marker_every_s`
    (hard cut at `HARD_CUT_FACTOR` times that). Unpunctuated tracks (most auto captions)
    have no sentence ends to wait for, so they cut at the marker time itself.
    """
    ends = sum(1 for s in segments if _SENTENCE_END.search(s.text))
    punctuated = bool(segments) and ends / len(segments) >= PUNCTUATED_SHARE
    out: list[tuple[float, str]] = []
    start: float | None = None
    buf: list[str] = []
    for seg in segments:
        if start is not None and buf:
            elapsed = seg.start - start
            if elapsed >= marker_every_s and (
                not punctuated or _SENTENCE_END.search(buf[-1]) or elapsed >= marker_every_s * HARD_CUT_FACTOR
            ):
                out.append((start, " ".join(buf)))
                start, buf = None, []
        if start is None:
            start = seg.start
        buf.append(seg.text)
    if start is not None and buf:
        out.append((start, " ".join(buf)))
    return out


def clean_vtt(vtt_text: str, marker_every_s: int = 60, duration_s: float | None = None) -> str:
    """Readable transcript: paragraphs prefixed with `[mm:ss]`, separated by blank lines.

    `duration_s` (from the video metadata) decides the hour format even when captions
    stop before the hour mark; without it the last cue end decides. Returns "" when the
    track has no usable text.
    """
    segments, last_end = parse_segments(vtt_text)
    if not segments:
        return ""
    hours = max(duration_s or 0.0, last_end) >= HOUR
    return "\n\n".join(f"[{format_ts(t, hours)}] {text}" for t, text in paragraphs(segments, max(marker_every_s, 1)))
