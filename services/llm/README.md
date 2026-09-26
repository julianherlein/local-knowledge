# kb-llm

The only way kb-engine talks to a model. Every call goes to the local Claude Code CLI
(`claude -p`), never to a hosted API, so there is no API key to manage and usage counts
against your Claude Code plan.

## Contract

`kb_llm.contract`:

- `LLMRequest(system, prompt, json_schema=None, model=None, timeout_s=600)`
- `LLMResponse(text, data, model, cost_usd, duration_ms, input_tokens, output_tokens)`
- `LLMError(message, transient=True)`
- `LLMClient` protocol: `.model`, `.complete(request) -> LLMResponse`

When `json_schema` is set, `data` is the validated object (from `structured_output`).

## Backends

| Backend | Use |
|---|---|
| `ClaudeCodeClient` | Production. Runs `claude -p --output-format json --tools "" --setting-sources "" --strict-mcp-config --no-session-persistence` in a temp dir, prompt on stdin. |
| `FakeLLMClient` | Tests. Returns whatever a responder callable returns. |

## Tests

```
uv run pytest services/llm            # gate tests, no network
KB_LIVE_LLM=1 uv run pytest services/llm -m live   # one real call (uses haiku)
```
