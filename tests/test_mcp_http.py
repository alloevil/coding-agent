"""
测试 MCP http/sse transport：transport 选择、SSE 解析、http 请求往返。
"""
import asyncio
import json

import pytest

from coding_agent.tools.mcp_client import (
    MCPClient, MCPError, _HttpTransport, _StdioTransport,
)


def test_transport_selection_http():
    c = MCPClient(name="x", url="https://example.com/mcp")
    assert isinstance(c._transport, _HttpTransport)


def test_transport_selection_stdio():
    c = MCPClient(name="x", command=["echo", "hi"])
    assert isinstance(c._transport, _StdioTransport)


def test_explicit_sse_transport():
    c = MCPClient(name="x", transport="sse", url="https://example.com/")
    assert isinstance(c._transport, _HttpTransport)


def test_http_without_url_errors():
    with pytest.raises(MCPError):
        MCPClient(name="x", transport="http")


def test_stdio_without_command_errors():
    with pytest.raises(MCPError):
        MCPClient(name="x", transport="stdio")


def test_sse_parser_picks_matching_id():
    sse = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n'
        "\n"
    )
    result = _HttpTransport._parse_sse(sse, 1)
    assert result == {"tools": []}


def test_sse_parser_error_raises():
    sse = 'data: {"jsonrpc":"2.0","id":1,"error":{"code":-1,"message":"boom"}}\n'
    with pytest.raises(MCPError):
        _HttpTransport._parse_sse(sse, 1)


def test_sse_parser_no_match_raises():
    sse = 'data: {"jsonrpc":"2.0","id":99,"result":{}}\n'
    with pytest.raises(MCPError):
        _HttpTransport._parse_sse(sse, 1)


class _FakeResponse:
    def __init__(self, payload, content_type="application/json"):
        self._payload = payload
        self.headers = {"content-type": content_type}
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self.text)


class _FakeHttpx:
    """记录 POST、按 method 返回预设响应的假 httpx client。"""
    def __init__(self):
        self.posts = []

    async def post(self, url, headers=None, content=None):
        self.posts.append(json.loads(content))
        req = json.loads(content)
        method = req.get("method")
        rid = req.get("id")
        if method == "tools/list":
            return _FakeResponse({"jsonrpc": "2.0", "id": rid,
                                  "result": {"tools": [{"name": "ping",
                                                        "description": "p",
                                                        "inputSchema": {"type": "object"}}]}})
        if method == "tools/call":
            return _FakeResponse({"jsonrpc": "2.0", "id": rid,
                                  "result": {"content": [{"type": "text", "text": "pong"}]}})
        # initialize / others
        return _FakeResponse({"jsonrpc": "2.0", "id": rid, "result": {}})

    async def aclose(self):
        pass


def test_http_list_and_call(monkeypatch):
    c = MCPClient(name="remote", url="https://example.com/mcp")
    fake = _FakeHttpx()
    # 注入假 client，跳过真实 httpx.AsyncClient
    c._transport._client = fake

    async def no_start():
        return None
    c._transport.start = no_start  # 已有 _client，无需真 start

    tools = asyncio.run(c.list_tools())
    assert tools[0]["name"] == "ping"
    out = asyncio.run(c.call_tool("ping", {}))
    assert out == "pong"
