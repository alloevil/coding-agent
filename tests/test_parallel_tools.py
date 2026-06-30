"""
测试只读工具的并发执行
"""
import asyncio
import time
import pytest

from coding_agent.core.agent import AgentLoop, AgentEvent
from coding_agent.core.config import AgentConfig
from coding_agent.core.state import AgentState
from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.base import Tool, ToolPermission


class _SlowReadTool(Tool):
    """只读工具，execute 里 sleep，用于检测并发。"""

    def __init__(self, name: str, delay: float = 0.3):
        self._name = name
        self._delay = delay

    @property
    def name(self): return self._name

    @property
    def description(self): return "slow read"

    @property
    def parameters(self): return {"type": "object", "properties": {}}

    @property
    def permission(self): return ToolPermission.READ

    async def execute(self, **kwargs):
        await asyncio.sleep(self._delay)
        return f"{self._name} done"


class _WriteTool(_SlowReadTool):
    @property
    def permission(self): return ToolPermission.WRITE


def _make_agent(tmp_path, tools):
    cfg = AgentConfig(model="m", api_key="k", auto_approve=True,
                      session_db_path=str(tmp_path / "s.db"), max_turns=4)
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return AgentLoop(config=cfg, tool_registry=reg), reg


def _tc(name, cid):
    return {"id": cid, "function": {"name": name, "arguments": "{}"}}


@pytest.mark.asyncio
async def test_readonly_tools_run_concurrently(tmp_path):
    agent, _ = _make_agent(tmp_path, [
        _SlowReadTool("r1", 0.3), _SlowReadTool("r2", 0.3), _SlowReadTool("r3", 0.3)
    ])
    turns = [
        {"content": "", "tool_calls": [_tc("r1", "1"), _tc("r2", "2"), _tc("r3", "3")]},
        {"content": "done", "tool_calls": []},
    ]
    idx = {"i": 0}
    async def model(ctx, tools):
        r = turns[idx["i"]]; idx["i"] += 1; return r
    agent.set_model_call_fn(model)

    state = AgentState(session_id="t")
    start = time.monotonic()
    results = []
    async for ev in agent.run(state, "go"):
        if ev.event == AgentEvent.TOOL_RESULT:
            results.append(ev.data["id"])
    elapsed = time.monotonic() - start

    # 3 个各 0.3s 的只读工具并发 → 应远小于 0.9s（串行）
    assert elapsed < 0.7, f"took {elapsed:.2f}s, not concurrent"
    # 结果按原始顺序返回，配对完整
    assert results == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_write_tools_stay_serial_and_ordered(tmp_path):
    agent, _ = _make_agent(tmp_path, [
        _WriteTool("w1", 0.2), _WriteTool("w2", 0.2)
    ])
    turns = [
        {"content": "", "tool_calls": [_tc("w1", "1"), _tc("w2", "2")]},
        {"content": "done", "tool_calls": []},
    ]
    idx = {"i": 0}
    async def model(ctx, tools):
        r = turns[idx["i"]]; idx["i"] += 1; return r
    agent.set_model_call_fn(model)

    state = AgentState(session_id="t")
    order = []
    async for ev in agent.run(state, "go"):
        if ev.event == AgentEvent.TOOL_RESULT:
            order.append(ev.data["id"])
    assert order == ["1", "2"]


@pytest.mark.asyncio
async def test_mixed_read_write_preserves_pairing(tmp_path):
    agent, _ = _make_agent(tmp_path, [
        _SlowReadTool("r1", 0.1), _WriteTool("w1", 0.1), _SlowReadTool("r2", 0.1)
    ])
    turns = [
        {"content": "", "tool_calls": [_tc("r1", "1"), _tc("w1", "2"), _tc("r2", "3")]},
        {"content": "done", "tool_calls": []},
    ]
    idx = {"i": 0}
    async def model(ctx, tools):
        r = turns[idx["i"]]; idx["i"] += 1; return r
    agent.set_model_call_fn(model)

    state = AgentState(session_id="t")
    async for _ in agent.run(state, "go"):
        pass
    # 每个 assistant tool_call 都应有对应 tool result（顺序一致）
    tool_msgs = [m for m in state.messages if m.tool_result]
    ids = [m.tool_result.tool_call_id for m in tool_msgs]
    assert ids == ["1", "2", "3"]
