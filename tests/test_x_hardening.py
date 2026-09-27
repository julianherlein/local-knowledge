"""X unit hardening: one test per finding of the X critic that the original suite missed."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
from test_x_support import install_keyring, light_settings, x_settings

from kb.capture import x_auth
from kb.fetchers import x as xf
from kb.queue import Queue


@pytest.fixture
def queue():
    q = Queue(":memory:")
    yield q
    q.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_unescape_happens_before_link_expansion():
    """M1: '&region=us' inside an expanded URL must not become '(R)egion=us'."""
    target = "https://ex.com/p?id=1&region=us&copy=2&not=3"
    got = xf.expand_text("Tom &amp; Jerry https://t.co/abc", [{"url": "https://t.co/abc", "expanded_url": target}])
    assert got.text == f"Tom & Jerry {target}"
    assert got.outlinks == [target]


def test_idle_connection_cannot_hold_the_callback_past_the_deadline(queue, monkeypatch):
    """M2: a speculative preconnect socket that never sends a request."""
    monkeypatch.setattr(x_auth, "HANDLER_TIMEOUT_S", 0.5)
    port = _free_port()
    s = x_settings(light_settings(), redirect_uri=f"http://127.0.0.1:{port}/callback")
    holders: list[socket.socket] = []

    def printer(line: str) -> None:
        if line.startswith("https://x.com/i/oauth2/authorize"):

            def hold() -> None:
                c = socket.create_connection(("127.0.0.1", port))
                holders.append(c)  # connected, never sends a byte

            threading.Thread(target=hold, daemon=True).start()

    t0 = time.monotonic()
    with pytest.raises(x_auth.XAuthError):
        x_auth.authorize(s, queue, open_browser=False, printer=printer, timeout_s=1.0)
    assert time.monotonic() - t0 < 4
    for c in holders:
        c.close()


def test_state_comparison_accepts_non_ascii():
    """L1: compare_digest raised TypeError on non-ASCII str input."""
    assert x_auth.state_matches("é", "abc") is False
    assert x_auth.state_matches(None, "abc") is False
    assert x_auth.state_matches("abc", "abc") is True


def test_non_json_token_response_is_a_typed_error():
    """L3."""
    resp = httpx.Response(200, text="<html>maintenance</html>")
    with pytest.raises(x_auth.XAuthError, match="non-JSON"):
        x_auth._json_object(resp, "the X token endpoint")
    with pytest.raises(x_auth.XAuthError, match="unexpected JSON"):
        x_auth._json_object(httpx.Response(200, json=[1, 2]), "the X token endpoint")


def test_backfilled_reply_without_metadata_is_incomplete():
    """L5: without metadata we cannot tell a self-reply from a reply to someone else."""
    rec = {
        "id": "1834000000000000000",
        "full_text": "and another thing",
        "screen_name": "jdoe",
        "name": "Jane Doe",
        "in_reply_to": "1833999999999999999",
        "created_at": "2025-03-02 11:15:00 -03:00",
    }
    _, thread = xf.export_post(rec, None)
    assert thread == "incomplete"


def test_keyring_save_clears_the_stale_state_copy(queue, monkeypatch):
    """Surviving mutation: the old refresh token in the state table is dead after rotation."""
    install_keyring(monkeypatch)
    s = x_settings(light_settings())
    queue.set_state("x.refresh_token", "OLD")
    assert x_auth.save_refresh_token(s, queue, "NEW") == "keyring"
    assert queue.get_state("x.refresh_token") is None
    assert x_auth.load_refresh_token(s, queue) == "NEW"
