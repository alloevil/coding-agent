"""
测试 Anthropic Messages 协议适配（OpenAI 形态 ↔ Anthropic 形态翻译 + SSE 累积）。

纯离线：不需要真端点。
"""
import json

from coding_agent.core.anthropic_protocol import (
    to_anthropic_request,
    from_anthropic_response,
    AnthropicStreamAccumulator,
    _tool_to_anthropic,
)


def test_system_extracted_to_top_level():
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ]
    req = to_anthropic_request(msgs, None)
    assert req["system"] == "You are helpful."
    # system 不应出现在 messages 里
    assert all(m["role"] != "system" for m in req["messages"])
    assert req["messages"][0] == {"role": "user", "content": "hi"}


def test_multiple_system_joined():
    msgs = [
        {"role": "system", "content": "A"},
        {"role": "system", "content": "B"},
        {"role": "user", "content": "x"},
    ]
    req = to_anthropic_request(msgs, None)
    assert req["system"] == "A\n\nB"


def test_assistant_tool_call_to_tool_use():
    msgs = [
        {"role": "assistant", "content": "let me check",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "grep", "arguments": '{"pattern":"x"}'}}]},
    ]
    req = to_anthropic_request(msgs, None)
    blocks = req["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "let me check"}
    assert blocks[1] == {"type": "tool_use", "id": "t1", "name": "grep",
                         "input": {"pattern": "x"}}


def test_tool_result_to_user_block():
    msgs = [{"role": "tool", "tool_call_id": "t1", "content": "found it"}]
    req = to_anthropic_request(msgs, None)
    block = req["messages"][0]
    assert block["role"] == "user"
    assert block["content"][0] == {"type": "tool_result",
                                   "tool_use_id": "t1", "content": "found it"}


def test_tool_definition_translation():
    tool = {"type": "function", "function": {
        "name": "grep", "description": "search",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}}}}
    out = _tool_to_anthropic(tool)
    assert out["name"] == "grep"
    assert out["description"] == "search"
    assert out["input_schema"]["properties"]["pattern"]["type"] == "string"


def test_from_response_text_and_tools():
    data = {
        "content": [
            {"type": "text", "text": "Here you go."},
            {"type": "tool_use", "id": "u1", "name": "file_read",
             "input": {"path": "a.py"}},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    out = from_anthropic_response(data)
    assert out["content"] == "Here you go."
    assert out["tool_calls"][0]["function"]["name"] == "file_read"
    assert json.loads(out["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}
    assert out["usage"]["prompt_tokens"] == 10
    assert out["usage"]["completion_tokens"] == 5


def test_usage_counts_cache_reads():
    data = {"content": [], "usage": {"input_tokens": 8, "output_tokens": 2,
                                     "cache_read_input_tokens": 100}}
    out = from_anthropic_response(data)
    assert out["usage"]["prompt_tokens"] == 108  # input + cache_read
    assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 100


def test_stream_accumulator_text():
    acc = AnthropicStreamAccumulator()
    acc.feed({"type": "content_block_start", "index": 0,
              "content_block": {"type": "text"}})
    acc.feed({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "Hel"}})
    acc.feed({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "lo"}})
    assert acc.result()["content"] == "Hello"


def test_stream_accumulator_thinking_delta():
    """extended-thinking：thinking_delta 累积到 reasoning + 触发回调，与正文分开。"""
    got = []
    acc = AnthropicStreamAccumulator(on_reasoning_delta=lambda c: got.append(c))
    acc.feed({"type": "content_block_delta", "index": 0,
              "delta": {"type": "thinking_delta", "thinking": "step 1 "}})
    acc.feed({"type": "content_block_delta", "index": 0,
              "delta": {"type": "thinking_delta", "thinking": "step 2"}})
    acc.feed({"type": "content_block_delta", "index": 1,
              "delta": {"type": "text_delta", "text": "the answer"}})
    r = acc.result()
    assert r["reasoning"] == "step 1 step 2"
    assert r["content"] == "the answer"   # reasoning 不混入正文
    assert got == ["step 1 ", "step 2"]   # 逐字回调


def test_stream_accumulator_tool_use():
    acc = AnthropicStreamAccumulator()
    acc.feed({"type": "content_block_start", "index": 0,
              "content_block": {"type": "tool_use", "id": "t1", "name": "grep"}})
    acc.feed({"type": "content_block_delta", "index": 0,
              "delta": {"type": "input_json_delta", "partial_json": '{"pat'}})
    acc.feed({"type": "content_block_delta", "index": 0,
              "delta": {"type": "input_json_delta", "partial_json": 'tern":"x"}'}})
    tc = acc.result()["tool_calls"][0]
    assert tc["function"]["name"] == "grep"
    assert json.loads(tc["function"]["arguments"]) == {"pattern": "x"}


def test_stream_accumulator_text_delta_callback():
    chunks = []
    acc = AnthropicStreamAccumulator(on_text_delta=chunks.append)
    acc.feed({"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "hi"}})
    assert chunks == ["hi"]


def test_roundtrip_via_model_client_protocol_attr():
    from coding_agent.core.model_client import ModelClient
    c = ModelClient(api_key="k", base_url="https://api.anthropic.com",
                    model="claude-opus-4-8", protocol="anthropic")
    payload = c._anthropic_payload(
        [{"role": "system", "content": "sys"},
         {"role": "user", "content": "hi"}], None, stream=False)
    assert payload["system"] == "sys"
    assert payload["model"] == "claude-opus-4-8"
    assert payload["messages"][0]["content"] == "hi"
    # headers 用 x-api-key 而非 Bearer
    h = c._anthropic_headers()
    assert h["x-api-key"] == "k"
    assert "anthropic-version" in h
