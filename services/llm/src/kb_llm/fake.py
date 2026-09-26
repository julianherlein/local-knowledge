"""Deterministic in-process backend for tests. Never used in production."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .contract import LLMError, LLMRequest, LLMResponse

Responder = Callable[[LLMRequest], dict[str, Any] | str | Exception]


class FakeLLMClient:
    def __init__(self, responder: Responder, model: str = "fake-model") -> None:
        self.model = model
        self.responder = responder
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        out = self.responder(request)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, str):
            if request.json_schema is not None:
                raise LLMError("fake returned text for a structured request")
            return LLMResponse(text=out, model=request.model or self.model, cost_usd=0.0)
        return LLMResponse(text="", data=out, model=request.model or self.model, cost_usd=0.0)
