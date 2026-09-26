"""WebVTT caption cleaning for YouTube transcripts (SDD §7.2).

YouTube serves two shapes of VTT and the same code has to handle both:

- Auto captions "roll": every cue repeats the line already on screen and adds a new
  line whose words carry inline timing tags (`hello<00:00:01.360><c> world</c>`), and
  short 10ms cues freeze the finished line. Read naively, every phrase appears twice.
- Manual captions are plain cues with no repetition. Two identical consecutive cues are
  real speech ("No." / "No.") and must be kept.

So the file is classified once: inline timing tags anywhere mean rolling captions, and
only then are cue lines that repeat the tail of what was already emitted dropped. The
result is merged into paragraphs with a `[mm:ss]` marker roughly every `marker_every_s`
seconds (`[h:mm:ss]` once the video reaches an hour), which keeps the transcript
skimmable and lets a reader jump to the moment in the video.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

import webvtt
from webvtt.errors import MalformedCaptionError, MalformedFileError

_INLINE_TS = re.compile(r"<(?:\d+:)?\d{2}:\d{2}\.\d{3}>")
_TAG = re.compile(r"<[^>]*>")
_WHITESPACE_LINE = re.compile(r"^[ \t]+\r?\n", re.MULTILINE)
_SENTENCE_END = re.compile(r"[.!?…][\"')\]]*$")

HOUR = 3600
# A paragraph that never reaches a sentence end (auto captions are often unpunctuated)
# is cut anyway at this multiple of the marker interval, so markers stay "roughly" regular.
HARD_CUT_FACTOR = 1.5


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


def _clean_line(line: str) -> str:
    return " ".join(html.unescape(_TAG.sub("", line)).split())


def parse_segments(vtt_text: str) -> tuple[list[Segment], float]:
    """Cue text as de-duplicated segments, plus the end time of the last cue.

    Returns ([], 0.0) for text that is not valid WebVTT, so a broken download reads the
    same as an empty track to the caller.
    """
    # YouTube auto captions put a lone " " line between a cue's timing and its new words.
    # webvtt-py 0.5.1 splits blocks on whitespace-only lines, which would orphan the
    # timing line and silently drop the words, so those lines are removed first.
    vtt_text = _WHITESPACE_LINE.sub("", vtt_text)
    try:
        captions = webvtt.from_string(vtt_text).captions
    except (MalformedFileError, MalformedCaptionError, ValueError):
        return [], 0.0
    rolling = any(_INLINE_TS.search(c.raw_text) for c in captions)
    segments: list[Segment] = []
    emitted: list[str] = []
    last_end = 0.0
    for cue in captions:
        last_end = max(last_end, _seconds(cue.end))
        lines = [cl for cl in (_clean_line(raw) for raw in cue.lines) if cl]
        if rolling:
            lines = lines[_overlap(emitted, lines) :]
        if not lines:
            continue
        emitted.extend(lines)
        segments.append(Segment(_seconds(cue.start), " ".join(lines)))
    return segments, last_end


def _seconds(ts: str) -> float:
    # webvtt's start_in_seconds truncates to int; keep the milliseconds for spacing math.
    parts = ts.split(":")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def _overlap(emitted: list[str], lines: list[str]) -> int:
    """Largest k such that the first k cue lines equal the last k emitted lines."""
    for k in range(min(len(emitted), len(lines)), 0, -1):
        if emitted[-k:] == lines[:k]:
            return k
    return 0


def paragraphs(segments: list[Segment], marker_every_s: int) -> list[tuple[float, str]]:
    """Group segments into (start, text) paragraphs of roughly `marker_every_s` seconds.

    A paragraph closes at the first sentence end after `marker_every_s`, or at the first
    segment boundary after `HARD_CUT_FACTOR * marker_every_s` when nothing is punctuated.
    """
    out: list[tuple[float, str]] = []
    start: float | None = None
    buf: list[str] = []
    for seg in segments:
        if start is not None and buf:
            elapsed = seg.start - start
            ends_sentence = bool(_SENTENCE_END.search(buf[-1]))
            if (elapsed >= marker_every_s and ends_sentence) or elapsed >= marker_every_s * HARD_CUT_FACTOR:
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
