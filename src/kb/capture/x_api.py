"""Small X API v2 client shared by the bookmark poller and the post fetcher (SDD §7.2, §8.1).

Why a wrapper instead of raw httpx calls: every X request needs the same three things,
and getting any of them wrong costs money or loses data:

- a fresh access token (refreshed 60s before expiry, so a long bookmark walk that
  crosses the 2h token lifetime keeps working),
- exactly one forced refresh + retry on HTTP 401 (a token revoked server-side before
  its expiry), never a loop,
- rate-limit errors turned into a typed exception that carries the reset time, so
  callers can stop cleanly instead of burning retries.

Tokens never appear in exception messages or logs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from ..config import Settings
from ..queue import Queue
from . import x_auth

API_BASE = "https://api.x.com/2"


class XApiError(Exception):
    """A non-2xx X API response (or a network failure when `status` is None)."""

    def __init__(self, message: str, *, status: int | None = None, reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.reset_at = reset_at

    @property
    def transient(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


class RateLimited(XApiError):
    """HTTP 429. `reset_at` comes from the `x-rate-limit-reset` header (epoch seconds)."""


def _reset_at(resp: httpx.Response) -> datetime | None:
    raw = resp.headers.get("x-rate-limit-reset")
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC) if raw else None
    except (TypeError, ValueError, OverflowError):
        return None


def _detail(resp: httpx.Response) -> str:
    """A short, token-free description of an error body."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        parts = [str(body.get(k)) for k in ("title", "detail", "error", "error_description") if body.get(k)]
        if not parts and isinstance(body.get("errors"), list) and body["errors"]:
            e = body["errors"][0]
            parts = [str(e.get("message") or e.get("detail") or e.get("title") or e)]
        return ": ".join(parts)[:300]
    return str(body)[:200]


def format_reset(reset_at: datetime | None) -> str:
    if not reset_at:
        return "unknown reset time"
    return "resets at " + reset_at.astimezone().strftime("%Y-%m-%d %H:%M")


class XClient:
    def __init__(self, settings: Settings, queue: Queue, http: httpx.Client) -> None:
        self.settings = settings
        self.queue = queue
        self.http = http
        self.requests = 0
        """Requests sent, for cost visibility in reports."""

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET `API_BASE + path`. Raises RateLimited, XApiError or x_auth.XAuthError."""
        token = x_auth.get_access_token(self.settings, self.queue, self.http)
        resp = self._send(path, params, token)
        if resp.status_code == 401:
            token = x_auth.get_access_token(self.settings, self.queue, self.http, force_refresh=True)
            resp = self._send(path, params, token)
        if resp.status_code == 429:
            reset = _reset_at(resp)
            raise RateLimited(f"X rate limit hit on {path}; {format_reset(reset)}", status=429, reset_at=reset)
        if resp.status_code >= 400:
            raise XApiError(f"X API {resp.status_code} on {path}: {_detail(resp)}", status=resp.status_code)
        try:
            data = resp.json()
        except ValueError as e:
            raise XApiError(f"X API returned non-JSON on {path}", status=resp.status_code) from e
        if not isinstance(data, dict):
            raise XApiError(f"X API returned unexpected JSON on {path}", status=resp.status_code)
        return data

    def _send(self, path: str, params: dict[str, Any] | None, token: str) -> httpx.Response:
        self.requests += 1
        try:
            return self.http.get(
                API_BASE + path,
                params=params,
                headers={"Authorization": f"Bearer {token}", "User-Agent": "kb-engine"},
            )
        except httpx.HTTPError as e:
            raise XApiError(f"network error calling X {path}: {type(e).__name__}") from e
