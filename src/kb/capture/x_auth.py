"""X OAuth 2.0 Authorization Code flow with PKCE (SDD §8.1, §6.2, §13).

`kb auth x` runs the browser flow once. After that every run uses the refresh token.

Why the storage order matters: X rotates the refresh token on every refresh and the
old one stops working immediately. If we used the new access token and crashed
before saving the new refresh token, the user would have to re-authorize. So the
new refresh token is persisted first, then the access token.

Storage:
- refresh token: OS keyring (service `kb-engine`, key `x.refresh_token`) when
  `x.use_keyring` is on and a backend works; otherwise the `state` table. A keyring
  that raises (no backend on a headless Linux box, a locked macOS keychain) flips the
  store to `state` and remembers that in `x.token_store`, so later runs do not
  retry a broken backend on every call. `kb auth x` tries the keyring again.
- access token + expiry: `state` keys `x.access_token`, `x.access_expires_at` (they
  live 2h; losing one only costs a refresh).
- the user: `x.user_id`, `x.username` (cached so polls skip a `/users/me` call).
"""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ..config import Settings
from ..queue import Queue

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
ME_URL = "https://api.x.com/2/users/me"

KEYRING_SERVICE = "kb-engine"
REFRESH_KEY = "x.refresh_token"
ACCESS_KEY = "x.access_token"
EXPIRES_KEY = "x.access_expires_at"
STORE_KEY = "x.token_store"
USER_ID_KEY = "x.user_id"
USERNAME_KEY = "x.username"
WATERMARK_KEY = "x.last_bookmark_id"

EXPIRY_SKEW = timedelta(seconds=60)
CALLBACK_TIMEOUT_S = 300.0
REAUTH = "run `kb auth x` to authorize again"


class XAuthError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


# PKCE (RFC 7636) ---------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def code_challenge(verifier: str) -> str:
    """S256 challenge: BASE64URL(SHA256(ASCII(verifier))), no padding."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def make_pkce() -> tuple[str, str]:
    """A 43-char verifier (32 random bytes) and its S256 challenge."""
    verifier = _b64url(secrets.token_bytes(32))
    return verifier, code_challenge(verifier)


def build_authorize_url(settings: Settings, state: str, challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.x_client_id or "",
        "redirect_uri": settings.x.redirect_uri,
        "scope": " ".join(settings.x.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# Token storage -----------------------------------------------------------------------
def _keyring_enabled(settings: Settings, queue: Queue) -> bool:
    return settings.x.use_keyring and queue.get_state(STORE_KEY) != "state"


def _fall_back(queue: Queue) -> None:
    queue.set_state(STORE_KEY, "state")


def load_refresh_token(settings: Settings, queue: Queue) -> str | None:
    if _keyring_enabled(settings, queue):
        try:
            import keyring

            tok = keyring.get_password(KEYRING_SERVICE, REFRESH_KEY)
            if tok:
                return tok
        except Exception:  # any backend failure: use the state table from now on
            _fall_back(queue)
    return queue.get_state(REFRESH_KEY)


def save_refresh_token(settings: Settings, queue: Queue, token: str) -> str:
    """Persist the (rotated) refresh token. Returns where it went: keyring | state."""
    if _keyring_enabled(settings, queue):
        try:
            import keyring

            keyring.set_password(KEYRING_SERVICE, REFRESH_KEY, token)
            # Read back: a null/no-op backend "succeeds" and would silently lose the
            # rotated token, which is the one thing this function must never do.
            if keyring.get_password(KEYRING_SERVICE, REFRESH_KEY) != token:
                raise RuntimeError("keyring did not keep the token")
            queue.set_state(REFRESH_KEY, None)  # never leave a stale copy behind
            queue.set_state(STORE_KEY, "keyring")
            return "keyring"
        except Exception:
            _fall_back(queue)
    queue.set_state(REFRESH_KEY, token)
    queue.set_state(STORE_KEY, "state")
    return "state"


def _store_tokens(settings: Settings, queue: Queue, tok: dict[str, Any], now: datetime) -> str:
    access = tok.get("access_token")
    if not access:
        raise XAuthError("X token response had no access_token")
    refresh = tok.get("refresh_token")
    if refresh:  # first, so a crash after this line never strands a rotated token
        save_refresh_token(settings, queue, refresh)
    try:
        expires_in = int(tok.get("expires_in") or 7200)
    except (TypeError, ValueError):
        expires_in = 7200
    queue.set_state(ACCESS_KEY, access)
    queue.set_state(EXPIRES_KEY, (now + timedelta(seconds=expires_in)).isoformat(timespec="seconds"))
    return access


# Token endpoint ----------------------------------------------------------------------
def _token_request(settings: Settings, http: httpx.Client, data: dict[str, str]) -> httpx.Response:
    """POST to the token endpoint. Public clients send client_id in the body; confidential
    clients (X_CLIENT_SECRET set) authenticate with HTTP Basic."""
    if not settings.x_client_id:
        raise XAuthError("X_CLIENT_ID is not set in ~/.kb/.env")
    form = dict(data)
    auth: tuple[str, str] | None = None
    if settings.x_client_secret:
        auth = (settings.x_client_id, settings.x_client_secret)
    else:
        form["client_id"] = settings.x_client_id
    try:
        return http.post(TOKEN_URL, data=form, auth=auth, headers={"User-Agent": "kb-engine"})
    except httpx.HTTPError as e:
        raise XAuthError(f"network error calling the X token endpoint: {type(e).__name__}") from e


def _oauth_error(resp: httpx.Response) -> tuple[str, str]:
    try:
        body = resp.json()
    except ValueError:
        return "", resp.text[:200]
    if not isinstance(body, dict):
        return "", str(body)[:200]
    return str(body.get("error") or ""), str(body.get("error_description") or body.get("detail") or "")


def exchange_code(settings: Settings, queue: Queue, http: httpx.Client, code: str, verifier: str) -> str:
    resp = _token_request(
        settings,
        http,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.x.redirect_uri,
            "code_verifier": verifier,
        },
    )
    if resp.status_code != 200:
        err, desc = _oauth_error(resp)
        raise XAuthError(f"code exchange failed ({resp.status_code} {err}: {desc})".replace(" : ", " "))
    tok = resp.json()
    if not tok.get("refresh_token"):
        raise XAuthError("X returned no refresh token; the app must request the offline.access scope")
    return _store_tokens(settings, queue, tok, _now())


def _refresh(settings: Settings, queue: Queue, http: httpx.Client, now: datetime) -> str:
    rt = load_refresh_token(settings, queue)
    if not rt:
        raise XAuthError(f"X is not authorized yet; {REAUTH}")
    resp = _token_request(settings, http, {"grant_type": "refresh_token", "refresh_token": rt})
    if resp.status_code != 200:
        err, desc = _oauth_error(resp)
        # X answers a dead refresh token with invalid_request ("Value passed for the
        # token was invalid") rather than the RFC's invalid_grant; both mean re-auth.
        if err in ("invalid_grant", "invalid_request", "invalid_client", "unauthorized_client"):
            queue.set_state(ACCESS_KEY, None)
            queue.set_state(EXPIRES_KEY, None)
            raise XAuthError(f"X rejected the refresh token ({err}: {desc}); {REAUTH}")
        raise XAuthError(f"token refresh failed ({resp.status_code} {err} {desc})".strip())
    return _store_tokens(settings, queue, resp.json(), now)


def get_access_token(
    settings: Settings,
    queue: Queue,
    http: httpx.Client,
    *,
    force_refresh: bool = False,
    now: datetime | None = None,
) -> str:
    """A valid access token, refreshing it when it expires within 60s (or when forced)."""
    now = now or _now()
    if not force_refresh:
        access = queue.get_state(ACCESS_KEY)
        exp = queue.get_state(EXPIRES_KEY)
        if access and exp:
            try:
                expires_at = datetime.fromisoformat(exp)
            except ValueError:
                expires_at = None
            if expires_at and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at and expires_at - EXPIRY_SKEW > now:
                return access
    return _refresh(settings, queue, http, now)


def is_authorized(settings: Settings, queue: Queue) -> bool:
    return bool(load_refresh_token(settings, queue))


def fetch_me(http: httpx.Client, access_token: str) -> tuple[str, str]:
    """(user_id, username) for the token's owner."""
    try:
        resp = http.get(ME_URL, headers={"Authorization": f"Bearer {access_token}", "User-Agent": "kb-engine"})
    except httpx.HTTPError as e:
        raise XAuthError(f"network error calling /2/users/me: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise XAuthError(f"/2/users/me failed with HTTP {resp.status_code}")
    data = (resp.json() or {}).get("data") or {}
    if not data.get("id"):
        raise XAuthError("/2/users/me returned no user id")
    return str(data["id"]), str(data.get("username") or "")


def remember_user(queue: Queue, user_id: str, username: str) -> None:
    previous = queue.get_state(USER_ID_KEY)
    if previous and previous != user_id:
        # A different account: its bookmarks share nothing with the old watermark.
        queue.set_state(WATERMARK_KEY, None)
    queue.set_state(USER_ID_KEY, user_id)
    queue.set_state(USERNAME_KEY, username)


# Browser flow ------------------------------------------------------------------------
def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><meta charset='utf-8'><title>kb-engine</title>"
        "<body style='font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;line-height:1.5'>"
        f"<h2>{html.escape(title)}</h2><p>{html.escape(body)}</p></body>"
    ).encode()


class _CallbackServer(HTTPServer):
    # On Windows SO_REUSEADDR lets a second socket steal a port that is in use, so the
    # callback could go to another process. Elsewhere it only skips TIME_WAIT, which helps.
    allow_reuse_address = sys.platform != "win32"


def _wait_for_callback(host: str, port: int, path: str, timeout_s: float, on_ready: Callable[[], None]) -> dict:
    """Serve one OAuth callback on host:port and return its query parameters."""
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib name)
            parts = urlsplit(self.path)
            if parts.path != path or result:
                self.send_response(404)
                self.end_headers()
                return
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            result.update(q or {"error": "empty_callback"})
            ok = "code" in q and "error" not in q
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if ok:
                self.wfile.write(
                    _page("kb-engine is authorized", "You can close this tab and go back to the terminal.")
                )
            else:
                self.wfile.write(_page("Authorization failed", q.get("error_description") or q.get("error", "")))

        def log_message(self, format: str, *args: Any) -> None:  # keep the code out of stderr
            return

    try:
        server = _CallbackServer((host, port), Handler)
    except OSError as e:
        raise XAuthError(
            f"cannot listen on {host}:{port} ({e.strerror or e}); free the port or change x.redirect_uri "
            "in config.toml and the callback URL in your X app to match"
        ) from e
    server.timeout = 0.2
    try:
        on_ready()
        deadline = time.monotonic() + timeout_s
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result:
        raise XAuthError(f"no callback from X within {int(timeout_s)}s; run `kb auth x` again")
    return result


def authorize(
    settings: Settings,
    queue: Queue,
    *,
    open_browser: bool = True,
    printer: Callable[[str], Any] = print,
    http: httpx.Client | None = None,
    timeout_s: float = CALLBACK_TIMEOUT_S,
) -> str:
    """Run the PKCE browser flow, store the tokens, and return the @username."""
    if not settings.x_client_id:
        raise XAuthError("X_CLIENT_ID is not set in ~/.kb/.env (see the README for the X app setup)")
    redirect = urlsplit(settings.x.redirect_uri)
    if redirect.scheme != "http" or not redirect.hostname or not redirect.port:
        raise XAuthError(f"x.redirect_uri must look like http://127.0.0.1:8765/callback, got {settings.x.redirect_uri}")

    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(settings, state, challenge)

    def on_ready() -> None:
        printer("Open this URL to authorize kb-engine (waiting up to 5 minutes):")
        printer(url)
        if open_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    params = _wait_for_callback(redirect.hostname, redirect.port, redirect.path or "/", timeout_s, on_ready)
    if "error" in params:
        raise XAuthError(f"X returned {params['error']}: {params.get('error_description', '')}".rstrip(": "))
    if not secrets.compare_digest(params.get("state", ""), state):
        raise XAuthError("state mismatch in the OAuth callback (possible CSRF); run `kb auth x` again")

    own = http is None
    client = http or httpx.Client(timeout=30)
    try:
        queue.set_state(STORE_KEY, None)  # give a previously failing keyring another chance
        access = exchange_code(settings, queue, client, params["code"], verifier)
        user_id, username = fetch_me(client, access)
    finally:
        if own:
            client.close()
    remember_user(queue, user_id, username)
    return username


def auth_status(settings: Settings, queue: Queue) -> tuple[bool, str]:
    """For `kb doctor`. No network: checks that credentials are stored."""
    if not settings.x_client_id:
        return False, "X_CLIENT_ID not set"
    if not is_authorized(settings, queue):
        return False, "not authorized; run `kb auth x`"
    store = queue.get_state(STORE_KEY) or ("keyring" if settings.x.use_keyring else "state")
    who = queue.get_state(USERNAME_KEY)
    where = "OS keyring" if store == "keyring" else "kb.sqlite state table"
    detail = f"authorized as @{who}" if who else "authorized"
    detail += f"; refresh token in {where}"
    if not settings.x.enabled:
        detail += "; x.enabled = false, bookmark polling is off"
    return True, detail
