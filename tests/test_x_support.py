"""Helpers shared by the X test modules (no tests here). Imported as a plain module:
pytest puts tests/ on sys.path because the directory has no __init__.py."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from conftest import FIXTURES

from kb.capture import x_auth
from kb.config import Settings, load_settings
from kb.queue import Queue

X_FIXTURES = FIXTURES / "x"
API = "https://api.x.com/2"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def load(name: str) -> Any:
    return json.loads((X_FIXTURES / name).read_text(encoding="utf-8"))


class FakeKeyring:
    """In-memory stand-in for the `keyring` module. `broken=True` raises like a
    missing backend (keyring.errors.NoKeyringError is a RuntimeError subclass)."""

    def __init__(self, broken: bool = False) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.broken = broken
        self.calls = 0

    def get_password(self, service: str, key: str) -> str | None:
        self.calls += 1
        if self.broken:
            raise RuntimeError("No recommended backend was available")
        return self.store.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self.calls += 1
        if self.broken:
            raise RuntimeError("No recommended backend was available")
        self.store[(service, key)] = value


def install_keyring(monkeypatch, broken: bool = False) -> FakeKeyring:
    fake = FakeKeyring(broken)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return fake


def light_settings() -> Settings:
    """Settings without the temp dirs and vault repo the shared fixture builds: X code only
    uses the queue (in memory in these tests), so a never-created home is enough and keeps
    each test's setup under a few ms on Windows."""
    home = Path(tempfile.gettempdir()) / "kb-x-tests-never-created"
    return load_settings(home, vault_path=str(home / "vault"), telegram={"enabled": False})


def x_settings(settings: Settings, *, secret: str | None = None, **x: Any) -> Settings:
    xs = settings.x.model_copy(update={"enabled": True, **x})
    return settings.model_copy(update={"x_client_id": "client-123", "x_client_secret": secret, "x": xs})


def authorize_queue(settings: Settings, queue: Queue, *, expires_in_s: int = 3600) -> None:
    """Pretend `kb auth x` ran: stored refresh token, valid access token, cached user."""
    x_auth.save_refresh_token(settings, queue, "REFRESH-0")
    queue.set_state(x_auth.ACCESS_KEY, "ACCESS-0")
    exp = datetime.now(UTC) + timedelta(seconds=expires_in_s)
    queue.set_state(x_auth.EXPIRES_KEY, exp.isoformat(timespec="seconds"))
    queue.set_state(x_auth.USER_ID_KEY, "2244994945")
    queue.set_state(x_auth.USERNAME_KEY, "jdoe")
