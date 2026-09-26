import json
import os
import shutil

import pytest
from kb_llm import ClaudeCodeClient, FakeLLMClient, LLMError, LLMRequest
from kb_llm.claude_code import parse_output

SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}


def _ok(**over):
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"x": 1}',
        "structured_output": {"x": 1},
        "total_cost_usd": 0.0031,
        "duration_ms": 4635,
        "usage": {"input_tokens": 1192, "output_tokens": 378},
        "modelUsage": {"claude-haiku-4-5-20251001": {}},
    }
    payload.update(over)
    return json.dumps(payload)


def test_parse_structured_output():
    r = parse_output(0, _ok(), "", LLMRequest(system="s", prompt="p", json_schema=SCHEMA), "m")
    assert r.data == {"x": 1}
    assert r.model == "claude-haiku-4-5-20251001"
    assert r.cost_usd == pytest.approx(0.0031)
    assert r.input_tokens == 1192 and r.output_tokens == 378


def test_parse_falls_back_to_result_json_when_structured_missing():
    out = _ok(structured_output=None)
    r = parse_output(0, out, "", LLMRequest(system="s", prompt="p", json_schema=SCHEMA), "m")
    assert r.data == {"x": 1}


def test_parse_text_mode_has_no_data():
    r = parse_output(0, _ok(result="hello"), "", LLMRequest(system="s", prompt="p"), "m")
    assert r.text == "hello" and r.data is None


@pytest.mark.parametrize(
    "stdout",
    [
        "not json at all",
        _ok(is_error=True, result="Credit balance is too low"),
        _ok(subtype="error_max_turns"),
        _ok(structured_output=None, result="prose, not json"),
        _ok(structured_output=[1, 2]),
    ],
)
def test_parse_errors_raise_llm_error(stdout):
    with pytest.raises(LLMError):
        parse_output(1, stdout, "boom", LLMRequest(system="s", prompt="p", json_schema=SCHEMA), "m")


def test_argv_is_sandboxed_and_carries_schema():
    argv = ClaudeCodeClient("claude-opus-5-5").build_argv(LLMRequest(system="sys", prompt="p", json_schema=SCHEMA))
    joined = " ".join(argv)
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    # The user prompt must never be on the command line (Windows argv limit).
    assert " p " not in f" {joined} "


def test_argv_request_model_overrides_default():
    argv = ClaudeCodeClient("a").build_argv(LLMRequest(system="s", prompt="p", model="b"))
    assert argv[argv.index("--model") + 1] == "b"


def test_argv_too_long_is_permanent_error():
    with pytest.raises(LLMError) as e:
        ClaudeCodeClient("m").build_argv(LLMRequest(system="x" * 40_000, prompt="p"))
    assert e.value.transient is False


def test_missing_binary_is_permanent_error():
    client = ClaudeCodeClient("m", binary="definitely-not-a-real-binary-kb")
    with pytest.raises(LLMError) as e:
        client.complete(LLMRequest(system="s", prompt="p"))
    assert e.value.transient is False


def test_fake_client_records_calls():
    fake = FakeLLMClient(lambda req: {"x": 2})
    r = fake.complete(LLMRequest(system="s", prompt="p", json_schema=SCHEMA))
    assert r.data == {"x": 2} and len(fake.calls) == 1


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("KB_LIVE_LLM") or not shutil.which("claude"), reason="set KB_LIVE_LLM=1")
def test_live_structured_call():
    client = ClaudeCodeClient(os.environ.get("KB_LIVE_MODEL", "haiku"))
    r = client.complete(LLMRequest(system="Answer with the number only.", prompt="What is 2+3?", json_schema=SCHEMA))
    assert r.data == {"x": 5}
