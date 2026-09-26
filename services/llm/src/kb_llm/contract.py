"""The LLM contract. Every caller in kb-engine talks to this, never to a backend directly."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class LLMRequest(BaseModel):
    system: str
    prompt: str
    json_schema: dict[str, Any] | None = None
    """When set, the backend must return `data` validated against this schema."""
    model: str | None = None
    """Overrides the client's default model for this call."""
    timeout_s: float = 600.0


class LLMResponse(BaseModel):
    text: str
    data: dict[str, Any] | None = None
    model: str
    cost_usd: float | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class LLMError(RuntimeError):
    """Any failure to get a usable answer. `transient` hints whether a retry can help."""

    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


@runtime_checkable
class LLMClient(Protocol):
    model: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...
