"""YouTube fetcher: metadata and a cleaned transcript from subtitles (SDD §7.2).

yt-dlp is used as a library for metadata only (`download=False`). The subtitle track
is chosen here, by a pure function, and its VTT is downloaded with the shared httpx
client. That keeps the choice testable, avoids temp files, and lets tests mock every
byte of network with respx.

Track choice, most preferred first:
1. manual subtitles before auto captions (auto captions have no punctuation and
   mishear names);
2. the video's original language, when it matches one of `settings.youtube.sub_langs`;
3. then the order of `sub_langs` (default `en.*`, `es.*`).

Auto captions come with a machine translation into every language YouTube supports.
A translation of a transcription is worse than no transcript at all for a knowledge
base, so only original-language auto tracks count: yt-dlp labels them `<lang>-orig`,
and when that label is missing, tracks whose URL carries a `tlang` (translate-to)
parameter are dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ..models import FetchedItem, FetchError
from ..queue import Item
from . import vtt

DESCRIPTION_MAX_CHARS = 2000
ORIG_SUFFIX = "-orig"
NON_SPEECH_TRACKS = {"live_chat", "rechat"}

UPGRADE_HINT = "YouTube may have broken yt-dlp: run `uv lock --upgrade-package yt-dlp && uv sync`"

# Checked before _UNAVAILABLE: YouTube words its rate limit as "Video unavailable. This
# content isn't available, try again later", and one rate-limit window must not turn a
# whole backfill of videos into failed_permanent.
_RATE_LIMITED = re.compile(
    r"try again later|rate[- ]?limit|too many requests|\bhttp error 429\b|\b429\b",
    re.IGNORECASE,
)
_NOT_YET = re.compile(r"live event will begin|premieres? in|premiere will begin|is_upcoming", re.IGNORECASE)
# Live, upcoming or just-ended streams get their final captions later: retry, never give up.
NOT_YET_LIVE_STATUS = {"is_live", "is_upcoming", "post_live"}
# yt-dlp error texts that mean the video will not become fetchable by retrying.
# "Sign in to confirm you're not a bot" is deliberately absent: that one is transient.
_UNAVAILABLE = re.compile(
    r"private video|video unavailable|this video is (?:no longer |not )?available|has been removed"
    r"|been terminated|removed by the uploader|confirm your age|age[- ]restricted|inappropriate for some users"
    r"|members[- ]only|join this channel|available to this channel's members|available in your country"
    r"|copyright (?:claim|grounds)|does not exist|incomplete youtube id",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SubtitleChoice:
    lang: str
    """The track key as yt-dlp reports it, e.g. `en`, `en-GB`, `es-orig`."""
    kind: str  # manual | auto
    url: str

    @property
    def language(self) -> str:
        """ISO 639-1 base: `es-orig` -> `es`, `en-GB` -> `en`."""
        return base_lang(self.lang)


def base_lang(code: str) -> str:
    return code.removesuffix(ORIG_SUFFIX).replace("_", "-").split("-", 1)[0].lower()


def _vtt_url(formats: Any) -> str | None:
    for f in formats or []:
        if isinstance(f, dict) and f.get("ext") == "vtt" and f.get("url"):
            return f["url"]
    return None


def _is_translation(url: str) -> bool:
    return "tlang" in parse_qs(urlsplit(url).query)


def _lang_matches(pattern: str, code: str) -> bool:
    """yt-dlp `--sub-langs` semantics (regex); a malformed pattern from config.toml counts as a literal."""
    try:
        return re.fullmatch(pattern, code, re.IGNORECASE) is not None
    except re.error:
        return pattern.casefold() == code.casefold()


def _rank(code: str, patterns: list[str], orig_lang: str | None) -> int | None:
    """0 for the original language (if wanted), 1 + pattern index otherwise, None if unwanted."""
    base = code.removesuffix(ORIG_SUFFIX)
    matches = [i for i, p in enumerate(patterns) if _lang_matches(p, base)]
    if not matches:
        return None
    if orig_lang and base_lang(base) == base_lang(orig_lang):
        return 0
    return 1 + matches[0]


def _best(candidates: dict[str, str], patterns: list[str], orig_lang: str | None) -> tuple[str, str] | None:
    ranked = [
        (rank, len(code), code, url)
        for code, url in candidates.items()
        if (rank := _rank(code, patterns, orig_lang)) is not None
    ]
    if not ranked:
        return None
    *_, code, url = min(ranked)
    return code, url


def original_language(info: dict[str, Any]) -> str | None:
    """The spoken language: yt-dlp's `language`, else the base of an `-orig` auto track."""
    lang = info.get("language")
    if isinstance(lang, str) and lang.strip():
        return base_lang(lang.strip())
    for code in info.get("automatic_captions") or {}:
        if code.endswith(ORIG_SUFFIX):
            return base_lang(code)
    return None


def choose_subtitles(info: dict[str, Any], patterns: list[str]) -> SubtitleChoice | None:
    """Pick the transcript track for `info` (a yt-dlp info dict). Pure; None if nothing fits."""
    orig = original_language(info)

    manual = {
        code: url
        for code, fmts in (info.get("subtitles") or {}).items()
        if code not in NON_SPEECH_TRACKS and (url := _vtt_url(fmts))
    }
    if best := _best(manual, patterns, orig):
        return SubtitleChoice(best[0], "manual", best[1])

    auto_all = {code: url for code, fmts in (info.get("automatic_captions") or {}).items() if (url := _vtt_url(fmts))}
    labelled = {c: u for c, u in auto_all.items() if c.endswith(ORIG_SUFFIX)}
    auto = labelled or {c: u for c, u in auto_all.items() if not _is_translation(u)}
    if best := _best(auto, patterns, orig):
        return SubtitleChoice(best[0], "auto", best[1])
    return None


# Metadata helpers ----------------------------------------------------------------


def format_duration(seconds: float | int | None) -> str | None:
    if not seconds or seconds <= 0:
        return None
    return vtt.format_ts(seconds, hours=seconds >= vtt.HOUR)


def parse_upload_date(value: Any) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def trim_description(text: str | None, limit: int = DESCRIPTION_MAX_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > limit * 0.8 else limit].rstrip() + " [...]"


def render_body(info: dict[str, Any], transcript: str) -> str:
    duration = info.get("duration") or 0
    hours = duration >= vtt.HOUR
    parts: list[str] = []
    if desc := trim_description(info.get("description")):
        parts.append(f"## Description\n\n{desc}")
    chapters = [c for c in info.get("chapters") or [] if isinstance(c, dict) and c.get("title")]
    if chapters:
        lines = [
            f"- [{vtt.format_ts(c.get('start_time') or 0, hours)}] {' '.join(str(c['title']).split())}"
            for c in chapters
        ]
        parts.append("## Chapters\n\n" + "\n".join(lines))
    parts.append(f"## Transcript\n\n{transcript}")
    return "\n\n".join(parts) + "\n"


# Network --------------------------------------------------------------------------


def extract_info(url: str) -> dict[str, Any]:
    """yt-dlp metadata for one video, nothing downloaded. Tests monkeypatch this."""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        # Metadata and subtitles only: a video whose formats yt-dlp cannot select (DRM,
        # PO-token gated streams) must not fail the whole extraction.
        "ignore_no_formats_error": True,
        "check_formats": False,
        # Manual subtitles machine-translated into every language are pure noise here.
        "extractor_args": {"youtube": {"skip": ["translated_subs"]}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=False))


def _info(url: str) -> dict[str, Any]:
    from yt_dlp.utils import DownloadError

    try:
        info = extract_info(url)
    except DownloadError as e:
        msg = str(e).removeprefix("ERROR: ")
        if _RATE_LIMITED.search(msg):
            raise FetchError(f"rate-limited by YouTube, retry later: {msg}", reason="ytdlp_rate_limited") from e
        if _NOT_YET.search(msg):
            raise FetchError(f"video not available yet: {msg}", reason="not_yet_available") from e
        if _UNAVAILABLE.search(msg):
            raise FetchError(f"video unavailable: {msg}", permanent=True, reason="video_unavailable") from e
        raise FetchError(f"yt-dlp failed: {msg}. {UPGRADE_HINT}", reason="ytdlp_error") from e
    except Exception as e:  # yt-dlp internals (KeyError, ExtractorError, ...) after a site change
        raise FetchError(f"yt-dlp crashed: {type(e).__name__}: {e}. {UPGRADE_HINT}", reason="ytdlp_error") from e
    if not isinstance(info, dict):
        raise FetchError(f"yt-dlp returned no metadata. {UPGRADE_HINT}", reason="ytdlp_error")
    if info.get("_type") == "playlist":
        raise FetchError("URL resolved to a playlist, not a video", permanent=True, reason="video_unavailable")
    return info


def _download_vtt(http: httpx.Client, url: str, timeout: float) -> str:
    # Subtitle URLs are signed and expire, so every failure here is worth a retry:
    # the next attempt re-extracts fresh URLs.
    try:
        resp = http.get(url, timeout=timeout)
    except httpx.TimeoutException as e:
        raise FetchError(f"subtitle download timed out: {e}", reason="timeout") from e
    except httpx.HTTPError as e:
        raise FetchError(f"subtitle download failed: {type(e).__name__}: {e}", reason="network_error") from e
    if resp.status_code >= 400:
        code = resp.status_code
        raise FetchError(f"subtitle download returned HTTP {code}", reason=f"subtitle_http_{code}")
    return resp.text


def fetch(item: Item, ctx) -> FetchedItem:
    settings = ctx.settings
    info = _info(item.canonical_url)
    if info.get("live_status") in NOT_YET_LIVE_STATUS:
        raise FetchError(
            f"stream is {info['live_status']}: captions are not final yet, will retry",
            reason="not_yet_available",
        )

    choice = choose_subtitles(info, settings.youtube.sub_langs)
    if choice is None:
        raise FetchError(
            f"no subtitles matching {settings.youtube.sub_langs} (manual or original-language auto); "
            "audio transcription with Whisper is Phase 2",
            permanent=True,
            reason="no_subtitles",
        )

    duration = info.get("duration")
    raw_vtt = _download_vtt(ctx.http, choice.url, settings.web.timeout_s)
    transcript = vtt.clean_vtt(raw_vtt, settings.youtube.marker_every_s, duration_s=duration)
    if not transcript:
        # YouTube answers 200 with an empty body when it wants a PO token; that can pass.
        raise FetchError(
            f"subtitle track {choice.lang} ({choice.kind}) downloaded empty. {UPGRADE_HINT}",
            reason="subtitles_empty",
        )

    channel = info.get("channel") or info.get("uploader") or None
    extra: dict[str, Any] = {
        "video_id": info.get("id") or None,
        "channel": channel,
        "duration": format_duration(duration),
        "subtitles": choice.kind,
        "subtitle_lang": choice.lang,
    }
    return FetchedItem(
        source_type="youtube",
        url=item.canonical_url,
        title=" ".join(str(info.get("title") or "").split()) or f"YouTube video {info.get('id') or ''}".strip(),
        body=render_body(info, transcript),
        author=channel,
        published=parse_upload_date(info.get("upload_date")),
        language=choice.language,
        extra={k: v for k, v in extra.items() if v is not None},
    )
