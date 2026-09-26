"""YouTube fetcher and VTT cleaning: track choice, dedup, markers, error mapping. No network."""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import date

import httpx
import pytest
from conftest import FIXTURES
from test_fetch_web import make_item

from kb.config import Settings
from kb.fetchers import FetchContext, vtt, youtube
from kb.fetchers.youtube import choose_subtitles
from kb.models import FetchError
from kb.pipeline import make_http

YT = FIXTURES / "youtube"
URL = "https://youtube.com/watch?v=aBcDeFgHiJk"
AUTO_VTT = (YT / "auto_rolling.en.vtt").read_text(encoding="utf-8")
MANUAL_VTT = (YT / "manual.en.vtt").read_text(encoding="utf-8")
INFO_CHAPTERS = json.loads((YT / "info_chapters.json").read_text(encoding="utf-8"))
INFO_NOSUBS = json.loads((YT / "info_nosubs.json").read_text(encoding="utf-8"))
LANGS = ["en.*", "es.*"]


def track(code: str, translated_from: str | None = None, ext: str = "vtt") -> list[dict]:
    q = f"lang={translated_from}&tlang={code.split('-')[0]}" if translated_from else f"lang={code}"
    return [
        {"ext": "json3", "url": f"https://yt.test/tt?{q}&fmt=json3"},
        {"ext": ext, "url": f"https://yt.test/tt?{q}&fmt={ext}"},
    ]


def info(manual=(), auto=(), language=None, translated=()) -> dict:
    d = {
        "subtitles": {c: track(c) for c in manual},
        "automatic_captions": {c: track(c) for c in auto},
    }
    for code, src in translated:
        d["automatic_captions"][code] = track(code, translated_from=src)
    if language:
        d["language"] = language
    return d


def picked(d: dict, langs=LANGS) -> tuple[str, str] | None:
    c = choose_subtitles(d, langs)
    return (c.lang, c.kind) if c else None


# Track selection (pure) ------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (info(manual=["en"], auto=["en-orig", "en"], language="en"), ("en", "manual")),
        (info(manual=["es"], auto=["en-orig", "en"], language="en"), ("es", "manual")),  # manual beats auto
        (info(manual=["en", "es"], language="es"), ("es", "manual")),  # original language first
        (info(manual=["en", "es"]), ("en", "manual")),  # then sub_langs order
        (info(manual=["es", "en"], language="en-US"), ("en", "manual")),
        (info(manual=["fr", "en"], language="fr"), ("en", "manual")),  # original not wanted: sub_langs
        (info(manual=["en-GB", "en"]), ("en", "manual")),  # exact code over a regional variant
        (info(manual=["live_chat"], auto=["en-orig", "en"]), ("en-orig", "auto")),
        (info(auto=["es-orig", "es"], translated=[("en", "es")]), ("es-orig", "auto")),  # orig over translation
        (info(auto=["es-orig", "es"], translated=[("en", "es")], language="es"), ("es-orig", "auto")),
        (info(auto=["fr-orig", "fr"], translated=[("en", "fr"), ("es", "fr")]), None),  # only translations wanted
        (info(auto=["en"], translated=[("es", "en")]), ("en", "auto")),  # no -orig labels: drop tlang tracks
        (info(translated=[("en", "de"), ("es", "de")]), None),
        (info(), None),
        ({}, None),
    ],
    ids=lambda v: str(v) if isinstance(v, tuple) or v is None else "",
)
def test_choose_subtitles_matrix(case, expected):
    assert picked(case) == expected


def test_manual_track_without_vtt_format_is_skipped():
    d = info(auto=["en-orig"])
    d["subtitles"]["en"] = track("en", ext="srv3")
    assert picked(d) == ("en-orig", "auto")


def test_sub_langs_order_is_configurable():
    d = info(manual=["en", "es"])
    assert picked(d, ["es.*", "en.*"]) == ("es", "manual")
    assert picked(d, ["de.*"]) is None
    assert picked(d, ["es(", "en"]) == ("en", "manual")  # a broken regex in config never crashes


def test_choice_reports_iso_base_language():
    c = choose_subtitles(info(auto=["es-orig", "es"]), LANGS)
    assert c is not None and c.language == "es" and c.url.endswith("lang=es-orig&fmt=vtt")
    assert youtube.base_lang("en-GB") == "en" and youtube.base_lang("pt_BR") == "pt"


def test_original_language_falls_back_to_orig_track():
    assert youtube.original_language({"automatic_captions": {"de": [], "es-orig": []}}) == "es"
    assert youtube.original_language({"language": "EN-us"}) == "en"
    assert youtube.original_language({}) is None


# VTT cleaning (pure) ---------------------------------------------------------------


def test_auto_rolling_captions_dedup_exact_text():
    # Every phrase once, the genuine "fun fun fun" repetition kept, tags and entities gone.
    assert vtt.clean_vtt(AUTO_VTT) == (
        "[00:00] so today we're talking about Q&A and it's going to be fun fun fun [Music] okay >> welcome"
    )


def test_manual_captions_keep_real_repetition_and_merge_lines():
    assert vtt.clean_vtt(MANUAL_VTT) == (
        "[00:01] Welcome back to the channel. No. No. Today we look at the one-handed backhand & its grip.\n\n"
        "[01:02] Second part starts here."
    )


def test_lone_space_line_cue_without_freeze_keeps_its_words():
    # The last cue after a silence has no freeze cue repeating it; its words must survive.
    text = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000 align:start position:0%\n \n"
        "hello<00:00:01.500><c> there</c>\n\n"
        "00:00:40.000 --> 00:00:42.000 align:start position:0%\n \n"
        "final<00:00:41.000><c> words</c>\n"
    )
    assert vtt.clean_vtt(text) == "[00:01] hello there final words"


def test_crlf_vtt_is_handled():
    assert vtt.clean_vtt(AUTO_VTT.replace("\n", "\r\n")) == vtt.clean_vtt(AUTO_VTT)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "WEBVTT\n",
        "WEBVTT\n\nNOTE nothing here\n",
        "not a vtt file",
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<c> </c>\n",
    ],
)
def test_vtt_without_usable_cues_is_empty(text):
    assert vtt.clean_vtt(text) == ""


def test_entities_and_voice_tags_are_decoded():
    text = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Roger>Tom &amp; Jerry&#39;s &quot;show&quot; &lt;3</v>\n"
    assert vtt.clean_vtt(text) == '[00:01] Tom & Jerry\'s "show" <3'


def synthetic_vtt(n_cues: int, step: float, punct_every: int = 0, start_offset: float = 0.0) -> tuple[str, list[str]]:
    words, blocks = [], ["WEBVTT", ""]
    for i in range(n_cues):
        s = start_offset + i * step
        w = f"w{i}" + ("." if punct_every and (i + 1) % punct_every == 0 else "")
        words.append(w)
        blocks += [f"{vtt.format_ts(s, True).zfill(8)}.000 --> {vtt.format_ts(s + step, True).zfill(8)}.000", w, ""]
    return "\n".join(blocks), words


def markers(transcript: str) -> list[int]:
    out = []
    for m in re.finditer(r"^\[(?:(\d+):)?(\d+):(\d\d)\]", transcript, re.MULTILINE):
        h, mm, ss = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        out.append(h * 3600 + mm * 60 + ss)
    return out


def test_markers_break_at_sentence_ends_roughly_every_interval_without_losing_words():
    text, words = synthetic_vtt(n_cues=150, step=4, punct_every=3)  # 10 minutes
    out = vtt.clean_vtt(text, marker_every_s=60)
    ts = markers(out)
    gaps = [b - a for a, b in zip(ts, ts[1:], strict=False)]
    assert ts[0] == 0 and len(ts) >= 8
    assert all(60 <= g <= 60 + 3 * 4 for g in gaps), gaps
    assert all(p.rstrip().endswith(".") for p in out.split("\n\n")[:-1]), "breaks land on sentence ends"
    assert re.sub(r"\[[\d:]+\] ", "", out).split() == words


def test_unpunctuated_captions_are_cut_at_the_hard_limit():
    text, words = synthetic_vtt(n_cues=100, step=5)
    out = vtt.clean_vtt(text, marker_every_s=60)
    gaps = [b - a for a, b in zip(markers(out), markers(out)[1:], strict=False)]
    assert gaps and all(g == 90 for g in gaps), gaps
    assert re.sub(r"\[[\d:]+\] ", "", out).split() == words


def test_marker_interval_comes_from_argument():
    text, _ = synthetic_vtt(n_cues=60, step=5, punct_every=1)
    assert markers(vtt.clean_vtt(text, marker_every_s=30))[:3] == [0, 30, 60]


@pytest.mark.parametrize(
    ("secs", "hours", "expected"),
    [
        (0, False, "00:00"),
        (62.9, False, "01:02"),
        (3725, False, "62:05"),
        (3725, True, "1:02:05"),
        (5, True, "0:00:05"),
        (10800, True, "3:00:00"),
    ],
)
def test_format_ts(secs, hours, expected):
    assert vtt.format_ts(secs, hours) == expected


def test_hour_format_for_long_videos():
    text, _ = synthetic_vtt(n_cues=6, step=5, punct_every=1)
    assert vtt.clean_vtt(text, duration_s=4000).startswith("[0:00:00] ")
    assert vtt.clean_vtt(text, duration_s=3599).startswith("[00:00] ")
    late, _ = synthetic_vtt(n_cues=3, step=5, start_offset=3 * 3600 - 5)  # a 3-hour video
    assert markers(vtt.clean_vtt(late)) == [3 * 3600 - 5]
    assert vtt.clean_vtt(late).startswith("[2:59:55] ")


# Metadata helpers ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("secs", "expected"),
    [(754, "12:34"), (59, "00:59"), (3600, "1:00:00"), (10925, "3:02:05"), (0, None), (None, None)],
)
def test_format_duration(secs, expected):
    assert youtube.format_duration(secs) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [("20260814", date(2026, 8, 14)), ("20261341", None), ("2026-08-14", None), (None, None)]
)
def test_parse_upload_date(value, expected):
    assert youtube.parse_upload_date(value) == expected


def test_trim_description():
    long = "word " * 1000
    out = youtube.trim_description(long)
    assert len(out) <= youtube.DESCRIPTION_MAX_CHARS + 6 and out.endswith(" [...]")
    assert youtube.trim_description("  short  ") == "short"
    assert youtube.trim_description(None) == ""


# fetch() ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    c = make_http(Settings())
    yield c
    c.close()


@pytest.fixture
def ctx(client):
    return FetchContext(Settings(), client, None)  # type: ignore[arg-type]


@pytest.fixture
def fake_info(monkeypatch):
    """Serve a recorded info dict instead of calling YouTube. Returns a setter."""
    box: dict = {}

    def setter(d: dict | Exception):
        box["v"] = d

    def fake(url):
        box["url"] = url
        if isinstance(box["v"], Exception):
            raise box["v"]
        return copy.deepcopy(box["v"])

    monkeypatch.setattr(youtube, "extract_info", fake)
    setter.calls = box  # type: ignore[attr-defined]
    return setter


def vtt_url(d: dict, code: str, container: str = "subtitles") -> str:
    return next(f["url"] for f in d[container][code] if f["ext"] == "vtt")


def test_fetch_manual_subtitles_full_body(ctx, respx_mock, fake_info):
    fake_info(INFO_CHAPTERS)
    respx_mock.get(vtt_url(INFO_CHAPTERS, "en")).mock(return_value=httpx.Response(200, text=MANUAL_VTT))
    got = youtube.fetch(make_item(URL, "youtube"), ctx)

    assert fake_info.calls["url"] == URL
    assert got.source_type == "youtube" and got.url == URL
    assert got.title == "The one-handed backhand, explained"
    assert got.author == "Tennis Lab"
    assert got.published == date(2026, 8, 14)
    assert got.language == "en"
    assert got.extra == {
        "video_id": "aBcDeFgHiJk",
        "channel": "Tennis Lab",
        "duration": "12:34",
        "subtitles": "manual",
        "subtitle_lang": "en",
    }
    assert got.body == (
        "## Description\n\n"
        "In this video we break down the one-handed backhand: grip, preparation and contact point.\n\n"
        "Gear I use: https://example.com/gear\n\n"
        "## Chapters\n\n"
        "- [00:00] Intro\n- [01:02] Grip and preparation\n- [07:00] Contact point\n\n"
        "## Transcript\n\n"
        "[00:01] Welcome back to the channel. No. No. Today we look at the one-handed backhand & its grip.\n\n"
        "[01:02] Second part starts here.\n"
    )


def test_fetch_auto_original_language_track(ctx, respx_mock, fake_info):
    d = copy.deepcopy(INFO_CHAPTERS)
    d["subtitles"] = {}
    d["chapters"] = None
    d["description"] = ""
    fake_info(d)
    respx_mock.get(vtt_url(d, "en-orig", "automatic_captions")).mock(return_value=httpx.Response(200, text=AUTO_VTT))
    got = youtube.fetch(make_item(URL, "youtube"), ctx)
    assert got.extra["subtitles"] == "auto" and got.extra["subtitle_lang"] == "en-orig"
    assert got.language == "en"
    assert got.body == (
        "## Transcript\n\n[00:00] so today we're talking about Q&A and it's going to be fun fun fun [Music] okay >> welcome\n"
    )


def test_fetch_three_hour_video_uses_hour_markers(ctx, respx_mock, fake_info):
    d = copy.deepcopy(INFO_CHAPTERS)
    d["duration"] = 10925
    d["chapters"] = [{"start_time": 0, "title": "Start"}, {"start_time": 7503, "title": "Final set"}]
    fake_info(d)
    respx_mock.get(vtt_url(d, "en")).mock(return_value=httpx.Response(200, text=MANUAL_VTT))
    got = youtube.fetch(make_item(URL, "youtube"), ctx)
    assert got.extra["duration"] == "3:02:05"
    assert "- [0:00:00] Start\n- [2:05:03] Final set" in got.body
    assert "[0:00:01] Welcome back" in got.body and "[0:01:02] Second part" in got.body


def test_fetch_without_subtitles_is_permanent(ctx, fake_info):
    fake_info(INFO_NOSUBS)
    with pytest.raises(FetchError) as ei:
        youtube.fetch(make_item(URL, "youtube"), ctx)
    assert (ei.value.permanent, ei.value.reason) == (True, "no_subtitles")
    assert "Whisper" in str(ei.value) and "Phase 2" in str(ei.value)


def test_fetch_with_only_translated_auto_captions_is_no_subtitles(ctx, fake_info):
    fake_info({**INFO_NOSUBS, **info(auto=["fr-orig", "fr"], translated=[("en", "fr"), ("es", "fr")])})
    with pytest.raises(FetchError) as ei:
        youtube.fetch(make_item(URL, "youtube"), ctx)
    assert ei.value.reason == "no_subtitles"


def download_error(msg: str) -> Exception:
    from yt_dlp.utils import DownloadError

    return DownloadError(f"ERROR: [youtube] aBcDeFgHiJk: {msg}")


@pytest.mark.parametrize(
    ("msg", "permanent", "reason"),
    [
        ("Private video. Sign in if you've been granted access to this video", True, "video_unavailable"),
        ("Video unavailable. This video has been removed by the uploader", True, "video_unavailable"),
        ("Video unavailable", True, "video_unavailable"),
        ("Sign in to confirm your age. This video may be inappropriate for some users.", True, "video_unavailable"),
        (
            "Join this channel to get access to members-only content like this video, and other exclusive perks.",
            True,
            "video_unavailable",
        ),
        ("The uploader has not made this video available in your country", True, "video_unavailable"),
        (
            "Video unavailable. The uploader has not made this video available in your country",
            True,
            "video_unavailable",
        ),
        (
            "Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies for the authentication.",
            False,
            "ytdlp_error",
        ),
        (
            "Unable to extract yt initial data; please report this issue on https://github.com/yt-dlp/yt-dlp/issues",
            False,
            "ytdlp_error",
        ),
        ("This live event will begin in 3 hours.", False, "ytdlp_error"),
    ],
)
def test_download_error_mapping(ctx, fake_info, msg, permanent, reason):
    fake_info(download_error(msg))
    with pytest.raises(FetchError) as ei:
        youtube.fetch(make_item(URL, "youtube"), ctx)
    assert (ei.value.permanent, ei.value.reason) == (permanent, reason)
    if reason == "ytdlp_error":
        assert "uv lock --upgrade-package yt-dlp && uv sync" in str(ei.value)


def test_unexpected_ytdlp_crash_is_transient(ctx, fake_info):
    fake_info(KeyError("videoDetails"))
    with pytest.raises(FetchError) as ei:
        youtube.fetch(make_item(URL, "youtube"), ctx)
    assert (ei.value.permanent, ei.value.reason) == (False, "ytdlp_error")


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(503), "subtitle_http_503"),
        (httpx.Response(429), "subtitle_http_429"),
        (httpx.Response(404), "subtitle_http_404"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("dns"), "network_error"),
        (httpx.Response(200, text=""), "subtitles_empty"),
        (httpx.Response(200, text="WEBVTT\n\n"), "subtitles_empty"),
    ],
)
def test_subtitle_download_failures_are_transient(ctx, respx_mock, fake_info, response, reason):
    fake_info(INFO_CHAPTERS)
    route = respx_mock.get(vtt_url(INFO_CHAPTERS, "en"))
    route.mock(side_effect=response) if isinstance(response, Exception) else route.mock(return_value=response)
    with pytest.raises(FetchError) as ei:
        youtube.fetch(make_item(URL, "youtube"), ctx)
    assert (ei.value.permanent, ei.value.reason) == (False, reason)


def test_extract_info_calls_ytdlp_without_downloading(monkeypatch):
    seen: dict = {}

    class FakeYDL:
        def __init__(self, opts):
            seen["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            seen["call"] = (url, download)
            return {"id": "aBcDeFgHiJk"}

        def sanitize_info(self, d):
            return d

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    assert youtube.extract_info(URL) == {"id": "aBcDeFgHiJk"}
    assert seen["call"] == (URL, False)
    assert seen["opts"]["noplaylist"] and seen["opts"]["skip_download"] and seen["opts"]["quiet"]


def test_playlist_result_is_rejected(ctx, fake_info):
    fake_info({"_type": "playlist", "entries": []})
    with pytest.raises(FetchError) as ei:
        youtube.fetch(make_item(URL, "youtube"), ctx)
    assert (ei.value.permanent, ei.value.reason) == (True, "video_unavailable")


# Live ------------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("KB_LIVE_NET"), reason="set KB_LIVE_NET=1")
def test_live_fetch_real_video():
    # Rick Astley, "Never Gonna Give You Up" (official video): long-lived, manual English subs.
    url = "https://youtube.com/watch?v=dQw4w9WgXcQ"
    settings = Settings()
    with make_http(settings) as client:
        got = youtube.fetch(make_item(url, "youtube"), FetchContext(settings, client, None))  # type: ignore[arg-type]
    assert "Never Gonna Give You Up" in got.title
    assert got.extra["video_id"] == "dQw4w9WgXcQ"
    assert got.extra["subtitles"] == "manual" and got.language == "en"
    assert "## Transcript\n\n[00:" in got.body
    assert "never gonna give you up" in got.body.lower()
