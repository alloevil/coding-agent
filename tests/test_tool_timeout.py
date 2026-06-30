"""
测试 registry 层的单工具执行超时：挂死的工具不应冻结 agent。
"""
import asyncio

import pytest

from coding_agent.tools.base import Tool, ToolPermission
from coding_agent.tools.registry import ToolRegistry


class _SleepTool(Tool):
    def __init__(self, seconds: float, timeout_override=None):
        self._seconds = seconds
        if timeout_override is not None:
            self.timeout_seconds = timeout_override

    @property
    def name(self): return "sleeper"
    @property
    def description(self): return "sleeps"
    @property
    def parameters(self): return {"type": "object", "properties": {}, "required": []}
    @property
    def permission(self): return ToolPermission.READ

    async def execute(self, **kwargs):
        await asyncio.sleep(self._seconds)
        return "done sleeping"


class _FastTool(_SleepTool):
    @property
    def name(self): return "fast"


def test_timeout_triggers():
    reg = ToolRegistry(default_tool_timeout=0.1)
    reg.register(_SleepTool(seconds=5))
    out = asyncio.run(reg.execute_tool("sleeper", {}))
    assert "timed out" in out.lower()
    assert "0s" in out or "timed out after" in out.lower()


def test_fast_tool_completes():
    reg = ToolRegistry(default_tool_timeout=2.0)
    reg.register(_SleepTool(seconds=0.01))
    out = asyncio.run(reg.execute_tool("sleeper", {}))
    assert out == "done sleeping"


def test_timeout_disabled_when_none():
    reg = ToolRegistry(default_tool_timeout=None)
    reg.register(_SleepTool(seconds=0.05))
    out = asyncio.run(reg.execute_tool("sleeper", {}))
    assert out == "done sleeping"


def test_per_tool_override():
    # 工具自带 timeout_seconds 覆盖默认值
    reg = ToolRegistry(default_tool_timeout=10.0)
    reg.register(_SleepTool(seconds=5, timeout_override=0.1))
    out = asyncio.run(reg.execute_tool("sleeper", {}))
    assert "timed out" in out.lower()


def test_shell_exempt_from_default_timeout():
    # shell_exec 自计时，应被豁免（不被 registry 超时包裹）
    reg = ToolRegistry(default_tool_timeout=0.01)

    class _Shell(_SleepTool):
        @property
        def name(self): return "shell_exec"

    reg.register(_Shell(seconds=0.1))
    out = asyncio.run(reg.execute_tool("shell_exec", {}))
    # 没有被 0.01s 超时打断
    assert out == "done sleeping"


def test_effective_timeout_resolution():
    reg = ToolRegistry(default_tool_timeout=120.0)
    t = _SleepTool(seconds=0)
    assert reg._effective_timeout("sleeper", t) == 120.0
    # 自计时工具豁免
    assert reg._effective_timeout("shell_exec", t) is None
    # 覆盖为 0 → 不限制
    t2 = _SleepTool(seconds=0, timeout_override=0)
    assert reg._effective_timeout("sleeper", t2) is None
