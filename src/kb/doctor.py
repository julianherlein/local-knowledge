"""`kb doctor`: verify dependencies and credentials. Each check returns (name, ok, detail);
ok=None means "optional and not configured", which is not a failure."""

from __future__ import annotations

import shutil
import subprocess

from .config import Settings
from .queue import Queue

Check = tuple[str, bool | None, str]


def _cmd_version(argv: list[str]) -> str | None:
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        out = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr).strip() else "?"


def checks(s: Settings) -> list[Check]:
    out: list[Check] = []

    v = _cmd_version([s.llm.binary, "--version"])
    out.append(("claude CLI", v is not None, v or f"`{s.llm.binary}` not on PATH; install Claude Code"))
    out.append(("LLM model", True, f"{s.llm.model} via {s.llm.backend}"))

    g = _cmd_version(["git", "--version"])
    out.append(("git", g is not None, g or "git not on PATH"))

    try:
        import yt_dlp.version

        out.append(
            ("yt-dlp", True, yt_dlp.version.__version__ + " (upgrade: uv lock --upgrade-package yt-dlp && uv sync)")
        )
    except ImportError:
        out.append(("yt-dlp", False, "not installed; run uv sync"))

    vault = s.vault_path
    if not vault.exists():
        out.append(("vault", False, f"{vault} missing; run `kb init`"))
    elif not (vault / ".git").exists():
        out.append(("vault", False, f"{vault} is not a git repo; run `kb init`"))
    else:
        out.append(("vault", True, str(vault)))

    queue = Queue(s.db_path)
    try:
        out.append(("database", True, str(s.db_path)))
        out.extend(_telegram(s, queue))
        out.extend(_x(s, queue))
    finally:
        queue.close()
    return out


def _telegram(s: Settings, queue: Queue) -> list[Check]:
    if not s.telegram.enabled:
        return [("telegram", None, "disabled in config")]
    if not s.telegram_bot_token:
        return [("telegram", None, "TELEGRAM_BOT_TOKEN not set in ~/.kb/.env (create a bot with @BotFather)")]
    from .capture import CaptureContext, telegram
    from .pipeline import make_http

    with make_http(s) as http:
        try:
            lines = telegram.check(CaptureContext(s, http, queue))
        except Exception as e:  # doctor reports, never crashes
            return [("telegram", False, f"{type(e).__name__}: {e}")]
    ok = bool(s.telegram.allowed_chat_ids)
    detail = "; ".join(lines)
    if not ok:
        detail += "; set telegram.allowed_chat_ids in config.toml"
    return [("telegram", ok, detail)]


def _x(s: Settings, queue: Queue) -> list[Check]:
    if not s.x.enabled:
        return [("x", None, "disabled in config")]
    if not s.x_client_id:
        return [("x", None, "X_CLIENT_ID not set in ~/.kb/.env (create an app at developer.x.com)")]
    from .capture import x_auth

    try:
        ok, detail = x_auth.auth_status(s, queue)
    except Exception as e:
        return [("x", False, f"{type(e).__name__}: {e}")]
    return [("x", ok, detail)]
