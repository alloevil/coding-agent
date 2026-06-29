"""
测试统一模型客户端 ModelClient

使用 httpx.MockTransport 模拟 OpenAI 兼容 API，验证：
- 流式 SSE 解析（content + tool_calls 累积）
- on_text_delta 回调
- 非流式解析
- 可重试状态码的指数退避重试
"""
import json
import httpx
import pytest

from coding_agent.core.model_client import ModelClient, _accumulate_tool_calls


def _sse(chunks: list[dict]) -> bytes:
    lines = []
    for c in chunks:
        lines.append(f"data: {json.dumps(c)}")
    lines.append("data: [DONE]")
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture
def patch_async_client(monkeypatch):
    """让 ModelClient 内部的 httpx.AsyncClient 使用给定的 MockTransport。"""
    def _apply(transport: httpx.MockTransport):
        real_init = httpx.AsyncClient.__init__

        def patched_init(self, *args, **kwargs):
            kwargs["transport"] = transport
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    return _apply


def test_accumulate_tool_calls_concatenates_arguments():
    acc: list = []
    _accumulate_tool_calls(acc, [{"index": 0, "id": "call_1",
                                  "function": {"name": "file_read", "arguments": '{"pa'}}])
    _accumulate_tool_calls(acc, [{"index": 0,
                                  "function": {"arguments": 'th": "a.py"}'}}])
    assert len(acc) == 1
    assert acc[0]["id"] == "call_1"
    assert acc[0]["function"]["name"] == "file_read"
    assert acc[0]["function"]["arguments"] == '{"path": "a.py"}'


@pytest.mark.asyncio
async def test_stream_parses_content_and_invokes_callback(patch_async_client):
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
    ]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=_sse(chunks)))
    patch_async_client(transport)

    deltas = []
    client = ModelClient(api_key="k", base_url="http://x/v1", model="m")
    result = await client.complete([{"role": "user", "content": "hi"}],
                                   on_text_delta=deltas.append)
    assert result["content"] == "Hello"
    assert deltas == ["Hel", "lo"]
    assert result["tool_calls"] == []


@pytest.mark.asyncio
async def test_stream_parses_tool_calls(patch_async_client):
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "grep", "arguments": '{"pat'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'tern": "x"}'}}]}}]},
    ]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=_sse(chunks)))
    patch_async_client(transport)

    client = ModelClient(api_key="k", base_url="http://x/v1", model="m")
    result = await client.complete([{"role": "user", "content": "hi"}])
    assert len(result["tool_calls"]) == 1
    tc = result["tool_calls"][0]
    assert tc["function"]["name"] == "grep"
    assert json.loads(tc["function"]["arguments"]) == {"pattern": "x"}


@pytest.mark.asyncio
async def test_nonstream(patch_async_client):
    body = {"choices": [{"message": {"content": "done", "tool_calls": []}}]}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    patch_async_client(transport)

    client = ModelClient(api_key="k", base_url="http://x/v1", model="m")
    result = await client.complete([{"role": "user", "content": "hi"}], stream=False)
    assert result["content"] == "done"


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds(patch_async_client):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(200, content=_sse([{"choices": [{"delta": {"content": "ok"}}]}]))

    transport = httpx.MockTransport(handler)
    patch_async_client(transport)

    client = ModelClient(api_key="k", base_url="http://x/v1", model="m",
                         base_delay=0.0, max_retries=3)
    result = await client.complete([{"role": "user", "content": "hi"}])
    assert result["content"] == "ok"
    assert calls["n"] == 2  # 第一次 429，第二次成功


@pytest.mark.asyncio
async def test_no_retry_on_400(patch_async_client):
    transport = httpx.MockTransport(lambda req: httpx.Response(400, json={"error": "bad"}))
    patch_async_client(transport)

    client = ModelClient(api_key="k", base_url="http://x/v1", model="m",
                         base_delay=0.0, max_retries=3)
    with pytest.raises(httpx.HTTPStatusError):
        await client.complete([{"role": "user", "content": "hi"}])
