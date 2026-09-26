"""Backend that shells out to the local Claude Code CLI (`claude -p`).

The call is sandboxed as far as the CLI allows: no tools, no settings files, no MCP
servers, no session persistence, and a throwaway working directory so no CLAUDE.md
is discovered. The user text goes over stdin, so its size is not bounded by the
Windows command-line limit (~32k chars); only the short system prompt and the schema
travel as arguments.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from typing import Any

from .contract import LLMError, LLMRequest, LLMResponse

# Longest argv we allow before refusing (Windows CreateProcess caps at 32767 chars).
_MAX_ARGV_CHARS = 30_000


class ClaudeCodeClient:
    def __init__(self, model: str, *, binary: str = "claude", effort: str | None = None) -> None:
        self.model = model
        self.binary = binary
        self.effort = effort

    def build_argv(self, request: LLMRequest) -> list[str]:
        exe = shutil.which(self.binary) or self.binary
        argv = [
            exe,
            "-p",
            "--output-format",
            "json",
            "--model",
            request.model or self.model,
            "--tools",
            "",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--system-prompt",
            request.system,
        ]
        if self.effort:
            argv += ["--effort", self.effort]
        if request.json_schema is not None:
            argv += ["--json-schema", json.dumps(request.json_schema, separators=(",", ":"))]
        if sum(len(a) + 1 for a in argv) > _MAX_ARGV_CHARS:
            raise LLMError("system prompt + schema exceed the command-line limit", transient=False)
        return argv

    def complete(self, request: LLMRequest) -> LLMResponse:
        argv = self.build_argv(request)
        try:
            with tempfile.TemporaryDirectory(prefix="kb-llm-") as cwd:
                proc = subprocess.run(
                    argv,
                    input=request.prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=request.timeout_s,
                    cwd=cwd,
                )
        except FileNotFoundError as e:
            raise LLMError(
                f"Claude Code CLI not found ({self.binary}); install it or set llm.binary", transient=False
            ) from e
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude -p timed out after {request.timeout_s:.0f}s") from e
        return parse_output(proc.returncode, proc.stdout, proc.stderr, request, self.model)


def parse_output(returncode: int, stdout: str, stderr: str, request: LLMRequest, default_model: str) -> LLMResponse:
    """Turn `claude -p --output-format json` output into an LLMResponse. Pure, so it is unit-tested."""
    try:
        payload: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as e:
        tail = (stderr or stdout or "").strip()[-500:]
        raise LLMError(f"claude -p exited {returncode} without JSON output: {tail}") from e

    if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
        msg = payload.get("result") or payload.get("subtype") or "unknown error"
        raise LLMError(f"claude -p reported an error: {msg}")

    text = payload.get("result") or ""
    data: dict[str, Any] | None = None
    if request.json_schema is not None:
        data = payload.get("structured_output")
        if data is None:
            # Older CLIs put the JSON in `result` only.
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                raise LLMError("structured output requested but none returned") from e
        if not isinstance(data, dict):
            raise LLMError(f"structured output is not an object: {type(data).__name__}")

    usage = payload.get("usage") or {}
    model_usage = payload.get("modelUsage") or {}
    model = next(iter(model_usage), None) or request.model or default_model
    return LLMResponse(
        text=text,
        data=data,
        model=model,
        cost_usd=payload.get("total_cost_usd"),
        duration_ms=payload.get("duration_ms"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        raw=payload,
    )
