"""X OAuth 2.0 PKCE: challenge math, the local callback server, token exchange and rotation."""

from __future__ import annotations

import base64
import os
import socket
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import respx
from test_x_support import API, authorize_queue, install_keyring, light_settings, load, x_settings

from kb.capture import x_auth
from kb.queue import Queue

TOKEN = "https://api.x.com/2/oauth2/token"
_LIVE_HOME = os.environ.get("KB_HOME")  # read at import: the autouse env fixture clears it


@pytest.fixture
def settings():
    return light_settings()


@pytest.fixture
def queue():
    q = Queue(":memory:")
    yield q
    q.close()


@pytest.fixture
def keyring(monkeypatch):
    return install_keyring(monkeypatch)


@pytest.fixture
def xs(settings, keyring):
    return x_settings(settings)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# PKCE -------------------------------------------------------------------------------
def test_code_challenge_matches_rfc7636_appendix_b():
    assert x_auth.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_make_pkce_verifier_is_valid_and_random():
    v1, c1 = x_auth.make_pkce()
    v2, _ = x_auth.make_pkce()
    assert v1 != v2
    assert 43 <= len(v1) <= 128 and "=" not in v1
    assert set(v1) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    assert c1 == x_auth.code_challenge(v1)


def test_authorize_url_params(xs):
    url = x_auth.build_authorize_url(xs, "st4te", "ch4llenge")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://x.com/i/oauth2/authorize"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {
        "response_type": "code",
        "client_id": "client-123",
        "redirect_uri": "http://127.0.0.1:8765/callback",
        "scope": "tweet.read users.read bookmark.read offline.access",
        "state": "st4te",
        "code_challenge": "ch4llenge",
        "code_challenge_method": "S256",
    }


# Browser flow (real local server, simulated browser) ---------------------------------
def _run_authorize(settings, queue, *, callback, timeout_s=10.0):
    """Run authorize(); when it prints the URL, a thread plays the browser redirect.
    `callback(auth_query) -> dict` builds the redirect query from the authorize URL."""
    seen: dict = {}

    def browser(url: str) -> None:
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        seen["auth"] = q
        target = q["redirect_uri"] + "?" + urlencode(callback(q))
        try:
            with urllib.request.urlopen(target, timeout=5) as r:
                seen["page"] = (r.status, r.read().decode())
        except urllib.error.HTTPError as e:
            seen["page"] = (e.code, e.read().decode())

    def printer(line: str) -> None:
        if line.startswith("https://x.com/i/oauth2/authorize"):
            threading.Thread(target=browser, args=(line,), daemon=True).start()

    result = x_auth.authorize(settings, queue, open_browser=False, printer=printer, timeout_s=timeout_s)
    return result, seen


@respx.mock
def test_authorize_full_flow_stores_tokens_and_user(settings, queue, keyring):
    s = x_settings(settings, redirect_uri=f"http://127.0.0.1:{_free_port()}/callback")
    token_route = respx.post(TOKEN).mock(return_value=httpx.Response(200, json=load("token.json")))
    me_route = respx.get(f"{API}/users/me").mock(return_value=httpx.Response(200, json=load("users_me.json")))

    who, seen = _run_authorize(s, queue, callback=lambda q: {"state": q["state"], "code": "AUTH-CODE"})

    assert who == "jdoe"
    assert seen["page"][0] == 200 and "close this tab" in seen["page"][1]
    form = _form(token_route.calls.last.request)
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "AUTH-CODE"
    assert form["redirect_uri"] == s.x.redirect_uri
    assert form["client_id"] == "client-123"
    assert x_auth.code_challenge(form["code_verifier"]) == seen["auth"]["code_challenge"]
    assert seen["auth"]["code_challenge_method"] == "S256"
    assert me_route.calls.last.request.headers["Authorization"] == "Bearer ACCESS-1"
    assert keyring.store[("kb-engine", "x.refresh_token")] == "REFRESH-1"
    assert queue.get_state("x.refresh_token") is None  # never duplicated outside the keyring
    assert queue.get_state("x.access_token") == "ACCESS-1"
    assert queue.get_state("x.user_id") == "2244994945"
    assert queue.get_state("x.username") == "jdoe"


@respx.mock
def test_authorize_ignores_wrong_state_requests(settings, queue, keyring):
    """X critic L2: a forged callback must not kill the flow; it is ignored until the deadline."""
    s = x_settings(settings, redirect_uri=f"http://127.0.0.1:{_free_port()}/callback")
    token_route = respx.post(TOKEN).mock(return_value=httpx.Response(200, json=load("token.json")))
    with pytest.raises(x_auth.XAuthError, match="wrong state were ignored"):
        _run_authorize(s, queue, callback=lambda q: {"state": "forged", "code": "AUTH-CODE"})
    assert not token_route.called
    assert x_auth.load_refresh_token(s, queue) is None


def test_authorize_reports_user_denial(settings, queue, keyring):
    s = x_settings(settings, redirect_uri=f"http://127.0.0.1:{_free_port()}/callback")
    with pytest.raises(x_auth.XAuthError, match="access_denied"):
        _run_authorize(s, queue, callback=lambda q: {"state": q["state"], "error": "access_denied"})


def test_authorize_port_in_use_is_a_clear_error(settings, queue, keyring):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        s = x_settings(settings, redirect_uri=f"http://127.0.0.1:{port}/callback")
        with pytest.raises(x_auth.XAuthError, match="cannot listen"):
            x_auth.authorize(s, queue, open_browser=False, printer=lambda _: None, timeout_s=1)


def test_authorize_times_out_without_callback(settings, queue, keyring):
    s = x_settings(settings, redirect_uri=f"http://127.0.0.1:{_free_port()}/callback")
    with pytest.raises(x_auth.XAuthError, match="no callback"):
        x_auth.authorize(s, queue, open_browser=False, printer=lambda _: None, timeout_s=0.05)


def test_authorize_requires_client_id(settings, queue):
    with pytest.raises(x_auth.XAuthError, match="X_CLIENT_ID"):
        x_auth.authorize(settings, queue, open_browser=False, printer=lambda _: None)


# Token exchange ----------------------------------------------------------------------
@respx.mock
def test_exchange_public_client_sends_client_id_in_body(xs, queue, http):
    route = respx.post(TOKEN).mock(return_value=httpx.Response(200, json=load("token.json")))
    assert x_auth.exchange_code(xs, queue, http, "C", "V") == "ACCESS-1"
    req = route.calls.last.request
    assert "Authorization" not in req.headers
    assert _form(req)["client_id"] == "client-123"


@respx.mock
def test_exchange_confidential_client_uses_basic_auth(settings, keyring, queue, http):
    s = x_settings(settings, secret="s3cret")
    route = respx.post(TOKEN).mock(return_value=httpx.Response(200, json=load("token.json")))
    x_auth.exchange_code(s, queue, http, "C", "V")
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Basic " + base64.b64encode(b"client-123:s3cret").decode()
    assert "client_id" not in _form(req)


@respx.mock
def test_exchange_without_refresh_token_explains_offline_access(xs, queue, http):
    tok = {k: v for k, v in load("token.json").items() if k != "refresh_token"}
    respx.post(TOKEN).mock(return_value=httpx.Response(200, json=tok))
    with pytest.raises(x_auth.XAuthError, match="offline.access"):
        x_auth.exchange_code(xs, queue, http, "C", "V")


# Refresh ------------------------------------------------------------------------------
def _set_access(queue, expires_at: datetime) -> None:
    queue.set_state("x.access_token", "ACCESS-0")
    queue.set_state("x.access_expires_at", expires_at.isoformat(timespec="seconds"))


@respx.mock
def test_valid_access_token_is_reused_without_network(xs, queue, http):
    authorize_queue(xs, queue)
    now = datetime.now(UTC)
    _set_access(queue, now + timedelta(seconds=120))
    assert x_auth.get_access_token(xs, queue, http, now=now) == "ACCESS-0"  # respx: no route, no call


@respx.mock
def test_token_expiring_within_skew_is_refreshed(xs, queue, http, keyring):
    authorize_queue(xs, queue)
    now = datetime.now(UTC)
    _set_access(queue, now + timedelta(seconds=30))
    route = respx.post(TOKEN).mock(return_value=httpx.Response(200, json=load("token.json")))
    assert x_auth.get_access_token(xs, queue, http, now=now) == "ACCESS-1"
    form = _form(route.calls.last.request)
    assert form == {"grant_type": "refresh_token", "refresh_token": "REFRESH-0", "client_id": "client-123"}
    exp = datetime.fromisoformat(queue.get_state("x.access_expires_at"))
    assert abs((exp - now).total_seconds() - 7200) < 2


@respx.mock
def test_rotated_refresh_token_is_persisted_before_the_access_token(xs, queue, http, keyring, monkeypatch):
    authorize_queue(xs, queue)
    _set_access(queue, datetime.now(UTC) - timedelta(minutes=1))
    events: list[str] = []
    real_set_pw, real_set_state = keyring.set_password, queue.set_state
    monkeypatch.setattr(keyring, "set_password", lambda s, k, v: (events.append(f"keyring:{v}"), real_set_pw(s, k, v)))
    monkeypatch.setattr(queue, "set_state", lambda k, v: (events.append(f"state:{k}"), real_set_state(k, v)))
    respx.post(TOKEN).mock(return_value=httpx.Response(200, json=load("token.json")))

    x_auth.get_access_token(xs, queue, http)

    assert events.index("keyring:REFRESH-1") < events.index("state:x.access_token")
    assert keyring.store[("kb-engine", "x.refresh_token")] == "REFRESH-1"


@pytest.mark.parametrize(
    "body",
    [
        {"error": "invalid_grant", "error_description": "Refresh token expired"},
        {"error": "invalid_request", "error_description": "Value passed for the token was invalid."},
    ],
)
@respx.mock
def test_dead_refresh_token_asks_for_reauth(xs, queue, http, body):
    authorize_queue(xs, queue)
    _set_access(queue, datetime.now(UTC) - timedelta(minutes=1))
    respx.post(TOKEN).mock(return_value=httpx.Response(400, json=body))
    with pytest.raises(x_auth.XAuthError, match="kb auth x") as exc:
        x_auth.get_access_token(xs, queue, http)
    assert "REFRESH-0" not in str(exc.value)
    assert queue.get_state("x.access_token") is None


@respx.mock
def test_force_refresh_ignores_a_valid_token(xs, queue, http):
    authorize_queue(xs, queue)
    respx.post(TOKEN).mock(return_value=httpx.Response(200, json=load("token.json")))
    assert x_auth.get_access_token(xs, queue, http, force_refresh=True) == "ACCESS-1"


def test_refresh_without_any_token_says_not_authorized(xs, queue, http):
    with pytest.raises(x_auth.XAuthError, match="not authorized"):
        x_auth.get_access_token(xs, queue, http)


# Storage ------------------------------------------------------------------------------
def test_broken_keyring_falls_back_to_state(settings, queue, monkeypatch):
    install_keyring(monkeypatch, broken=True)
    s = x_settings(settings)
    assert x_auth.save_refresh_token(s, queue, "R1") == "state"
    assert queue.get_state("x.refresh_token") == "R1"
    assert x_auth.load_refresh_token(s, queue) == "R1"
    assert x_auth.save_refresh_token(s, queue, "R2") == "state"
    assert x_auth.load_refresh_token(s, queue) == "R2"


def test_one_failed_keyring_read_does_not_lock_the_user_out(settings, queue, monkeypatch):
    """X critic H1: a transient keyring failure used to flip the store to an empty state table."""
    fake = install_keyring(monkeypatch)
    s = x_settings(settings)
    assert x_auth.save_refresh_token(s, queue, "GOOD") == "keyring"
    fake.broken = True
    with pytest.raises(x_auth.KeyringUnavailable):  # e.g. a locked keychain during one run
        x_auth.load_refresh_token(s, queue)
    fake.broken = False
    assert x_auth.load_refresh_token(s, queue) == "GOOD"
    assert x_auth.is_authorized(s, queue)


def test_keyring_failing_on_read_falls_back(settings, queue, monkeypatch):
    s = x_settings(settings, use_keyring=False)
    x_auth.save_refresh_token(s, queue, "R1")  # stored in state while keyring was off
    install_keyring(monkeypatch, broken=True)
    s_on = x_settings(settings)
    assert x_auth.load_refresh_token(s_on, queue) == "R1"


def test_keyring_that_silently_drops_writes_falls_back(settings, queue, monkeypatch):
    fake = install_keyring(monkeypatch)
    monkeypatch.setattr(fake, "set_password", lambda *a: None)  # like keyring's null backend
    s = x_settings(settings)
    assert x_auth.save_refresh_token(s, queue, "R1") == "state"
    assert x_auth.load_refresh_token(s, queue) == "R1"


def test_use_keyring_false_uses_state(settings, queue, keyring):
    s = x_settings(settings, use_keyring=False)
    assert x_auth.save_refresh_token(s, queue, "R1") == "state"
    assert keyring.store == {}


def test_switching_accounts_resets_the_bookmark_watermark(queue):
    x_auth.remember_user(queue, "1", "a")
    queue.set_state("x.last_bookmark_id", "999")
    x_auth.remember_user(queue, "1", "a")
    assert queue.get_state("x.last_bookmark_id") == "999"
    x_auth.remember_user(queue, "2", "b")
    assert queue.get_state("x.last_bookmark_id") is None


def test_auth_status(settings, queue, keyring):
    assert x_auth.auth_status(settings, queue) == (False, "X_CLIENT_ID not set")
    s = x_settings(settings)
    ok, detail = x_auth.auth_status(s, queue)
    assert not ok and "kb auth x" in detail
    authorize_queue(s, queue)
    ok, detail = x_auth.auth_status(s, queue)
    assert ok and "@jdoe" in detail and "keyring" in detail


# Live smoke test -----------------------------------------------------------------------
@pytest.mark.live
@pytest.mark.skipif(os.environ.get("KB_LIVE_X") != "1", reason="set KB_LIVE_X=1 to call the real X API")
def test_live_users_me():  # pragma: no cover - network
    from kb.config import load_settings
    from kb.queue import Queue

    s = load_settings(_LIVE_HOME)
    q = Queue(s.db_path)
    try:
        with httpx.Client(timeout=30) as http:
            token = x_auth.get_access_token(s, q, http)
            user_id, username = x_auth.fetch_me(http, token)
    finally:
        q.close()
    assert user_id.isdigit() and username
