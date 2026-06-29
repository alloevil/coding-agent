"""
测试 ToolRegistry 对超长工具结果的截断
"""
import pytest

from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.base import Tool, ToolPermission


class _BigOutputTool(Tool):
    def __init__(self, payload: str):
        self._payload = payload

    @property
    def name(self): return "big"

    @property
    def description(self): return "emits a fixed payload"

    @property
    def parameters(self): return {"type": "object", "properties": {}}

    @property
    def permission(self): return ToolPermission.READ

    async def execute(self, **kwargs): return self._payload


@pytest.mark.asyncio
async def test_short_result_not_truncated():
    reg = ToolRegistry(max_result_chars=1000)
    reg.register(_BigOutputTool("hello"))
    out = await reg.execute_tool("big", {})
    assert out == "hello"


@pytest.mark.asyncio
async def test_long_result_truncated_head_and_tail():
    reg = ToolRegistry(max_result_chars=1000)
    payload = "H" * 5000 + "T" * 5000
    reg.register(_BigOutputTool(payload))
    out = await reg.execute_tool("big", {})
    assert len(out) < len(payload)
    assert out.startswith("H")
    assert out.endswith("T")
    assert "truncated by tool-output limit" in out


@pytest.mark.asyncio
async def test_truncation_can_be_disabled():
    reg = ToolRegistry(max_result_chars=0)
    payload = "X" * 100000
    reg.register(_BigOutputTool(payload))
    out = await reg.execute_tool("big", {})
    assert out == payload
