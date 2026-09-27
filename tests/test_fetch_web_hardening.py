# ruff: noqa: F811  (pytest fixtures imported from test_fetch_web)
"""Web fetcher hardening, one test per finding of the fetcher critic (walls, charsets, limits)."""

from __future__ import annotations

import httpx
import pytest
from test_fetch_web import FILLER, URL, client, ctx, html_response, make_item, page  # noqa: F401

from kb.fetchers import web
from kb.models import FetchError

WALL_TEXT = "<p>" + "We and our partners use cookies to personalise content and measure ads. " * 12 + "</p>"


@pytest.mark.parametrize(
    "target",
    [
        "https://consent.example.com/?continue=https://datanotes.example.com/posts/airflow-to-dagster",
        "https://datanotes.example.com/subscribe?from=/posts/airflow-to-dagster",
        "https://accounts.example.com/signin",
    ],
)
def test_realistic_wall_behind_redirect_is_login_wall(ctx, respx_mock, target):
    """M1: a long consent/subscribe page (well over min_chars) must not be saved as the article."""
    respx_mock.get(URL).mock(return_value=httpx.Response(302, headers={"location": target}))
    respx_mock.get(target).mock(return_value=html_response(page("<title>We value your privacy</title>", WALL_TEXT)))
    with pytest.raises(FetchError) as ei:
        web.fetch(make_item(), ctx)
    assert (ei.value.permanent, ei.value.reason) == (True, "login_wall")


def test_same_path_redirect_is_not_a_wall():
    assert not web.is_wall_redirect(URL, URL + "/")
    assert not web.is_wall_redirect(URL, URL.replace("https://", "https://www."))
    assert not web.is_wall_redirect("https://example.com/a", "https://example.com/authors/jane")
    assert web.is_wall_redirect("https://example.com/a", "https://example.com/auth/login")


def test_charset_only_in_header_is_honoured(ctx, respx_mock):
    """M2: latin-1 bytes, charset declared only in Content-Type, no <meta charset>."""
    body = page("<title>Réseau café</title>", "<p>" + "Le café est très bon à Paris. " * 30 + "</p>")
    respx_mock.get(URL).mock(return_value=html_response(body.encode("latin-1"), ctype="text/html; charset=ISO-8859-1"))
    got = web.fetch(make_item(), ctx)
    assert "très bon à Paris" in got.body


def test_body_over_the_byte_cap_is_rejected(ctx, respx_mock, monkeypatch):
    """H2: never buffer an unbounded body."""
    monkeypatch.setattr(web, "MAX_BYTES", 10_000)
    real = web.download
    monkeypatch.setattr(web, "download", lambda http, url, timeout: real(http, url, timeout, max_bytes=10_000))
    respx_mock.get(URL).mock(return_value=html_response(page(body=FILLER * 100)))
    with pytest.raises(FetchError) as ei:
        web.fetch(make_item(), ctx)
    assert (ei.value.permanent, ei.value.reason) == (True, "too_large")


def test_declared_content_length_rejected_before_reading(ctx, respx_mock):
    resp = httpx.Response(
        200, headers={"content-type": "text/html", "content-length": str(50 * 1024 * 1024)}, content=b"x"
    )
    respx_mock.get(URL).mock(return_value=resp)
    with pytest.raises(FetchError) as ei:
        web.fetch(make_item(), ctx)
    assert ei.value.reason == "too_large"


def test_media_type_rejected_from_headers(ctx, respx_mock):
    respx_mock.get(URL).mock(
        return_value=httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"\0" * 100)
    )
    with pytest.raises(FetchError) as ei:
        web.fetch(make_item(), ctx)
    assert (ei.value.permanent, ei.value.reason) == (True, "unsupported_content_type")


def test_wall_clock_deadline(ctx, respx_mock, monkeypatch):
    """H2: a server dripping bytes never trips per-read timeouts; the total deadline must."""
    ticks = iter(range(0, 1000, 10))  # every clock read advances 10s
    monkeypatch.setattr(web, "_clock", lambda: next(ticks))

    def drip():
        for _ in range(5):
            yield FILLER.encode()

    respx_mock.get(URL).mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}, content=drip()))
    with pytest.raises(FetchError) as ei:
        web.fetch(make_item(), ctx)
    assert (ei.value.permanent, ei.value.reason) == (False, "timeout")


def test_plain_text_without_charset_falls_back_to_cp1252():
    """L6."""
    assert web.decode_text("Café notes".encode("cp1252"), None) == "Café notes"
    assert web.decode_text("Café notes".encode(), None) == "Café notes"


def test_html_title_is_capped(ctx, respx_mock):
    """L5."""
    respx_mock.get(URL).mock(return_value=html_response(page(f"<title>{'Long title ' * 500}</title>")))
    got = web.fetch(make_item(), ctx)
    assert len(got.title) <= web.MAX_TITLE_CHARS


def test_451_is_permanent(ctx, respx_mock):
    """L8."""
    respx_mock.get(URL).mock(return_value=httpx.Response(451))
    with pytest.raises(FetchError) as ei:
        web.fetch(make_item(), ctx)
    assert (ei.value.permanent, ei.value.reason) == (True, "http_451")
