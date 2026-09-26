from .claude_code import ClaudeCodeClient
from .contract import LLMClient, LLMError, LLMRequest, LLMResponse
from .fake import FakeLLMClient


def make_client(backend: str, model: str, **kwargs) -> LLMClient:
    if backend == "claude_code":
        return ClaudeCodeClient(model, **kwargs)
    raise ValueError(f"unknown LLM backend {backend!r} (supported: claude_code)")


__all__ = [
    "ClaudeCodeClient",
    "FakeLLMClient",
    "LLMClient",
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "make_client",
]
