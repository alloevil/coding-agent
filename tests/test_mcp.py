"""
测试 MCP 客户端（用 tests/fixtures/mock_mcp_server.py 作为 stdio server）
"""
import sys
from pathlib import Path

import pytest

from coding_agent.tools.mcp_client import (
    MCPClient, MCPTool, register_mcp_servers, _render_tool_result,
)
from coding_agent.tools.registry import ToolRegistry

MOCK = str(Path(__file__).parent / "fixtures" / "mock_mcp_server.py")


def _server_cmd():
    return [sys.executable, MOCK]


@pytest.mark.asyncio
async def test_list_tools():
    c = MCPClient(name="mock", command=_server_cmd())
    try:
        tools = await c.list_tools()
        names = {t["name"] for t in tools}
        assert names == {"echo", "add"}
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_call_tool():
    c = MCPClient(name="mock", command=_server_cmd())
    try:
        out = await c.call_tool("echo", {"text": "hi"})
        assert out == "echo: hi"
        out2 = await c.call_tool("add", {"a": 2, "b": 3})
        assert out2 == "5"
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_register_mcp_servers_into_registry():
    reg = ToolRegistry()
    clients = await register_mcp_servers({"mock": {"command": _server_cmd()}}, reg)
    try:
        names = sorted(t.name for t in reg.get_all_tools())
        assert "mcp__mock__echo" in names
        assert "mcp__mock__add" in names
        # 调用包装后的工具
        tool = reg.get_tool("mcp__mock__echo")
        assert await tool.execute(text="world") == "echo: world"
        assert tool.permission.value == "execute"
    finally:
        for c in clients:
            await c.close()


@pytest.mark.asyncio
async def test_unavailable_server_degrades_gracefully():
    reg = ToolRegistry()
    # 不存在的命令：应跳过，不抛异常
    clients = await register_mcp_servers(
        {"broken": {"command": ["/nonexistent/binary/xyz"]}}, reg)
    assert clients == []
    assert reg.get_all_tools() == []


def test_render_tool_result_text_and_error():
    assert _render_tool_result({"content": [{"type": "text", "text": "ok"}]}) == "ok"
    assert _render_tool_result(
        {"content": [{"type": "text", "text": "bad"}], "isError": True}
    ).startswith("Error:")
