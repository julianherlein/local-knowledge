"""Web fetcher: status mapping, content types, extraction, metadata fallbacks. No network (respx)."""

from __future__ import annotations

import os
from dataclasses import fields
from datetime import date

import httpx
import pytest
from conftest import FIXTURES

from kb.config import Settings
from kb.fetchers import FetchContext, web
from kb.models import FetchError
from kb.pipeline import make_http
from kb.queue import Item

URL = "https://datanotes.example.com/posts/airflow-to-dagster"
ARTICLE_EN = (FIXTURES / "web" / "article_en.html").read_bytes()
ARTICLE_ES = (FIXTURES / "web" / "article_es.html").read_bytes()
FILLER = "<p>" + "A sentence of body text that trafilatura will keep as content. " * 12 + "</p>"


def make_item(url: str = URL, source_type: str = "web") -> Item:
    defaults = {f.name: None for f in fields(Item)}
    defaults.update(
        id=1,
        url=url,
        canonical_url=url,
        source_type=source_type,
        origin="cli",
        status="queued",
        domains=[],
        attempts=0,
        captured_at="2026-09-26T10:00:00+00:00",
        updated_at="2026-09-26T10:00:00+00:00",
    )
    return Item(**defaults)


@pytest.fixture(scope="module")
def client():
    # Building an httpx.Client loads the TLS trust store (~0.15s on Windows): once per module.
    c = make_http(Settings())
    yield c
    c.close()


@pytest.fixture
def ctx(client):
    return FetchContext(Settings(), client, None)  # type: ignore[arg-type]


def html_response(body: bytes | str, status: int = 200, ctype: str = "text/html; charset=utf-8") -> httpx.Response:
    return httpx.Response(
        status, content=body.encode() if isinstance(body, str) else body, headers={"content-type": ctype}
    )


def page(head: str = "", body: str = FILLER, lang: str | None = None) -> str:
    attr = f' lang="{lang}"' if lang else ""
    return f"<!DOCTYPE html><html{attr}><head>{head}</head><body><article>{body}</article></body></html>"


# Happy path ------------------------------------------------------------------------


def test_article_is_extracted_to_clean_markdown_without_noise(ctx, respx_mock):
    respx_mock.get(URL).mock(return_value=html_response(ARTICLE_EN))
    got = web.fetch(make_item(), ctx)

    assert got.source_type == "web"
    assert got.url == URL
    assert got.title == "Why we moved off Airflow"
    assert got.author == "Jane Doe"
    assert got.published == date(2026, 9, 20)
    assert got.language == "en"
    assert got.outlinks == []
    assert got.extra == {"sitename": "Data Notes", "hostname": "datanotes.example.com"}

    body = got.body
    assert not body.startswith("# Why we moved off Airflow"), "title must not be duplicated in the body"
    assert body.startswith("For five years our data platform ran on Apache Airflow.")
    assert "## The problem with task-centric orchestration" in body
    assert "[Dagster](https://dagster.io)" in body, "links stay inline"
    assert "| Failed runs per week | 45 | 27 |" in body, "tables are kept"
    assert "Failures dropped by 40 percent" in body
    # Source-HTML indentation inside a paragraph is joined back into one line.
    assert "replaced it with [Dagster](https://dagster.io) last spring, what the migration cost" in body
    for noise in (
        "cookie",
        "Subscribe to the newsletter",
        "Archive",
        "Related posts",
        "All rights reserved",
        "Privacy",
    ):
        assert noise not in body, noise
    assert body.endswith("\n") and not body.endswith("\n\n")


def test_non_english_latin1_page_decodes_and_reports_language(ctx, respx_mock):
    # Served without a charset: trafilatura must detect ISO-8859-1 from the bytes/meta tag.
    url = "https://tenishoy.example.es/tecnica/reves-una-mano"
    respx_mock.get(url).mock(return_value=html_response(ARTICLE_ES, ctype="text/html"))
    got = web.fetch(make_item(url), ctx)
    assert got.title == "El revés a una mano: guía técnica"
    assert got.author == "María López"
    assert got.published == date(2026, 8, 14)
    assert got.language == "es"
    assert "La mayoría de los jugadores profesionales utiliza la empuñadura eastern" in got.body
    assert "Inicio" not in got.body


def test_final_url_after_redirect_is_normalized(ctx, respx_mock):
    short = "https://sho.rt/abc"
    final = "https://www.datanotes.example.com/posts/airflow-to-dagster/?utm_source=x&fbclid=1&page=2"
    respx_mock.get(short).mock(return_value=httpx.Response(301, headers={"location": final}))
    respx_mock.get(final).mock(return_value=html_response(ARTICLE_EN))
    got = web.fetch(make_item(short), ctx)
    assert got.url == "https://datanotes.example.com/posts/airflow-to-dagster?page=2"


def test_plain_text_and_markdown_documents_are_used_verbatim(ctx, respx_mock):
    url = "https://raw.example.com/notes/README.md"
    doc = "# Notes on backfills\n\n" + "Backfills should be idempotent and partitioned by date. " * 12
    respx_mock.get(url).mock(return_value=html_response(doc, ctype="text/markdown; charset=utf-8"))
    got = web.fetch(make_item(url), ctx)
    assert got.title == "Notes on backfills"
    assert got.body == doc.strip() + "\n"
    assert got.extra["content_type"] == "text/markdown"


# Status and transport mapping ------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "permanent", "reason"),
    [
        (404, True, "http_404"),
        (410, True, "http_410"),
        (401, True, "http_401"),
        (403, False, "http_403"),
        (429, False, "http_429"),
        (500, False, "http_5xx"),
        (502, False, "http_5xx"),
        (503, False, "http_5xx"),
        (400, False, "http_400"),
    ],
)
def test_http_status_mapping(ctx, respx_mock, status, permanent, reason):
    respx_mock.get(URL).mock(return_value=httpx.Response(status, text="nope"))
    with pytest.raises(FetchError) as ei:
        web.fetch(make_item(), ctx)
    assert (ei.value.permanent, ei.value.reason) == (permanent, reason)
    assert str(status) in str(ei.value)


@pytest.mark.parametrize(
    ("exc", "permanent", "reason"),
    [
        (httpx.ConnectTimeout("slow"), False, "timeout"),
        (httpx.ReadTimeout("slow"), False, "timeout"),
        (httpx.ConnectError("dns"), False, "network_error"),
        (httpx.RemoteProtocolError("reset"), False, "network_error"),
        (httpx.TooManyRedirects("loop"), True, "too_many_redirects"),
    ],
)
def test_transport_errors_never_escape_raw(ctx, respx_mock, exc, permanent, reason):
    respx_mock.get(URL).mock(side_effect=exc)
    with pytest.raises(FetchError) as ei:
        web.fetch(make_item(), ctx)
    assert (ei.value.permanent, ei.value.reason) == (permanent, reason)


def test_redirect_loop_is_permanent(ctx, respx_mock):
    a, b = "https://loop.example.com/a", "https://loop.example.com/b"
    respx_mock.get(a).mock(return_value=httpx.Response(302, headers={"location": b}))
    respx_mock.get(b).mock(return_value=httpx.Response(302, headers={"location": a}))
    with pytest.raises(FetchError) as ei:
        web.fetch(make_item(a), ctx)
    assert (ei.value.permanent, ei.value.reason) == (True, "too_many_redirects")


# Content type and extraction -------------------------------------------------------


@pytest.mark.parametrize(
    ("ctype", "content"),
    [
        ("application/pdf", b"%PDF-1.7 ..."),
        ("image/png", b"\x89PNG\r\n"),
        ("application/json", b"{}"),
        ("", b"%PDF-1.4 sniffed without a header"),
    ],
)
def test_unsupported_content_types_are_permanent(ctx, respx_mock, ctype, content):
    headers = {"content-type": ctype} if ctype else {}
    respx_mock.get(URL).mock(return_value=httpx.Response(200, content=content, headers=headers))
    with pytest.raises(FetchError) as ei:
        web.fetch(make_item(), ctx)
    assert (ei.value.permanent, ei.value.reason) == (True, "unsupported_content_type")
    assert "Phase 2" in str(ei.value)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"   \n",
        page(body="<p>Subscribe to keep reading. This article is for paying members only.</p>").encode(),
        b'<html><body><div id="root"></div><script src="/app.js"></script></body></html>',
    ],
    ids=["empty", "whitespace", "paywall", "js-only"],
)
def test_short_or_empty_extraction_is_permanent_extraction_empty(ctx, respx_mock, body):
    respx_mock.get(URL).mock(return_value=html_response(body))
    with pytest.raises(FetchError) as ei:
        web.fetch(make_item(), ctx)
    assert (ei.value.permanent, ei.value.reason) == (True, "extraction_empty")
    assert "paywall" in str(ei.value)


def test_redirect_to_login_page_is_extraction_empty(ctx, respx_mock):
    login = "https://datanotes.example.com/login?next=/posts/airflow-to-dagster"
    respx_mock.get(URL).mock(return_value=httpx.Response(302, headers={"location": login}))
    respx_mock.get(login).mock(
        return_value=html_response(page("<title>Sign in</title>", "<form><p>Sign in to continue</p></form>"))
    )
    with pytest.raises(FetchError) as ei:
        web.fetch(make_item(), ctx)
    assert ei.value.reason == "extraction_empty"
    assert "login" in str(ei.value)


def test_min_chars_comes_from_settings(ctx, respx_mock):
    ctx.settings.web.min_chars = 50
    respx_mock.get(URL).mock(
        return_value=html_response(page("<title>Short note</title>", "<p>" + "Short but real content. " * 5 + "</p>"))
    )
    assert web.fetch(make_item(), ctx).title == "Short note"


# Metadata fallbacks ----------------------------------------------------------------


def test_title_prefers_og_title_then_title_tag(ctx, respx_mock):
    respx_mock.get(URL).mock(
        return_value=html_response(page('<meta property="og:title" content="OG Title"><title>Tag Title</title>'))
    )
    assert web.fetch(make_item(), ctx).title == "OG Title"


def test_title_falls_back_to_title_tag(ctx, respx_mock):
    respx_mock.get(URL).mock(return_value=html_response(page("<title>  Tag\n Title </title>")))
    assert web.fetch(make_item(), ctx).title == "Tag Title"


def test_title_falls_back_to_host_and_path(ctx, respx_mock):
    respx_mock.get(URL).mock(return_value=html_response(page()))
    got = web.fetch(make_item(), ctx)
    assert got.title == "datanotes.example.com/posts/airflow-to-dagster"
    assert got.author is None and got.published is None and got.language is None


def test_fallback_title_unit():
    assert web.fallback_title(None, "https://www.example.com/") == "example.com"
    assert web.fallback_title(None, "https://example.com/a/b/") == "example.com/a/b"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-20", date(2026, 9, 20)),
        ("2026-09-20T08:30:00", date(2026, 9, 20)),
        ("20/09/2026", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(value, expected):
    assert web.parse_date(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("en-US", "en"),
        ("pt_BR", "pt"),
        ("ES", "es"),
        ("de", "de"),
        ("x-klingon", None),
        ("eng", None),
        ("", None),
        (None, None),
    ],
)
def test_iso_language(value, expected):
    assert web.iso_language(value) == expected


def test_language_from_content_language_meta_and_og_locale(ctx, respx_mock):
    respx_mock.get(URL).mock(
        return_value=html_response(page('<meta http-equiv="Content-Language" content="fr-FR"><title>T</title>'))
    )
    assert web.fetch(make_item(), ctx).language == "fr"
    respx_mock.get(URL).mock(
        return_value=html_response(page('<meta property="og:locale" content="it_IT"><title>T</title>'))
    )
    assert web.fetch(make_item(), ctx).language == "it"


# Markdown tidying ------------------------------------------------------------------


def test_tidy_joins_indented_continuations_but_not_code_or_lists():
    md = (
        "# My Title\n\nMore text after it, which\n     wraps [in](http://c.d) the\n     source.\n\n"
        "```\ndef f():\n    return 1\n```\nAfter code.\n\n- one\n- two [link](http://a.b) and\n      more\n  - nested"
    )
    assert web.tidy_markdown(md, "my  title") == (
        "More text after it, which wraps [in](http://c.d) the source.\n\n"
        "```\ndef f():\n    return 1\n```\n\nAfter code.\n\n- one\n- two [link](http://a.b) and more\n  - nested"
    )


def test_tidy_keeps_a_leading_heading_that_is_not_the_title():
    assert web.tidy_markdown("# Intro\n\nText", "Something else") == "# Intro\n\nText"
    assert web.tidy_markdown("# Intro\n\nText", "Introduction to Rust") == "# Intro\n\nText"


@pytest.mark.parametrize(
    ("heading", "title", "expected"),
    [
        ("Tenis", "Tenis - Wikipedia, la enciclopedia libre", True),
        ("Why we moved off Airflow", "Why we moved off Airflow | Data Notes", True),
        ("Microservices", "microservices", True),
        ("Intro", "Introduction to Rust", False),
        ("Intro", "Rust: Intro", False),
        ("", "Anything", False),
    ],
)
def test_is_title_heading(heading, title, expected):
    assert web.is_title_heading(heading, title) is expected


def test_tidy_collapses_double_spaces_in_prose_only():
    md = "First sentence.  Second  one.\n\n```\nx  =  1\n```\n\n| a  | b |"
    assert web.tidy_markdown(md) == "First sentence. Second one.\n\n```\nx  =  1\n```\n\n| a  | b |"


def test_tidy_joins_source_wraps_and_unindents_paragraph_starts():
    md = (
        "a definition of this new term\n\n"
        "    *The term has sprung up over the\n    last few years to describe*\n\n\n\n"
        "In the summer of 1995, my friend and I\nstarted a startup called \n[Viaweb](http://v.w).  \nOur plan was"
    )
    assert web.tidy_markdown(md) == (
        "a definition of this new term\n\n"
        "*The term has sprung up over the last few years to describe*\n\n"
        "In the summer of 1995, my friend and I started a startup called [Viaweb](http://v.w). Our plan was"
    )


def test_tidy_never_glues_block_starts_and_leaves_tables_alone():
    md = (
        "## Heading\nText right under it\n> quote\n1. first\n2) second\n"
        "| Metric | Before |\n|---|---|\n| Failed runs | 45 |\n|  |  |\n\nEnd."
    )
    assert web.tidy_markdown(md) == (
        "## Heading\nText right under it\n> quote\n1. first\n2) second\n"
        "| Metric | Before |\n|---|---|\n| Failed runs | 45 |\n\nEnd."
    )


def test_tidy_keeps_an_empty_header_row_of_a_real_table():
    md = "|  |  |\n|---|---|\n| a | b |"
    assert web.tidy_markdown(md) == md


def test_layout_table_page_extracts_as_prose_with_links(ctx, respx_mock):
    url = "https://paulgraham.com/avg.html"
    respx_mock.get(url).mock(return_value=html_response((FIXTURES / "web" / "layout_table.html").read_bytes()))
    got = web.fetch(make_item(url), ctx)
    assert got.title == "Beating the Averages"
    assert "|" not in got.body, got.body[:300]
    assert got.body.startswith("April 2001, rev. April 2003\n\n")
    assert (
        "my friend Robert Morris and I started a startup called [Viaweb](http://docs.yahoo.com/docs/pr/release184.html)."
        in got.body
    )
    # Paragraphs stay separate and each is one line.
    assert "\n\nA lot of people could have been having this idea at the same time, of course, but" in got.body
    # No date markup on the page: the "summer of 1995" in the prose must not become a date.
    assert got.published is None


def test_is_layout_table():
    assert web.is_layout_table("| " + "prose " * 100 + "|")
    assert not web.is_layout_table("| Metric | Before |\n|---|---|\n| Failed runs | 45 |")
    assert not web.is_layout_table("plain " * 200)


# Live ------------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("KB_LIVE_NET"), reason="set KB_LIVE_NET=1")
def test_live_fetch_real_article(tmp_path):
    # Paul Graham's essays have been online, unchanged, for decades.
    settings = Settings(home=tmp_path)
    with make_http(settings) as client:
        got = web.fetch(make_item("https://paulgraham.com/avg.html"), FetchContext(settings, client, None))  # type: ignore[arg-type]
    assert "Beating the Averages" in got.title
    assert "Lisp" in got.body and len(got.body) > 5000
    assert got.url == "https://paulgraham.com/avg.html"
