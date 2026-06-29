"""
测试重试事件能通过 AgentLoop.run() 实时透出
"""
import pytest

from coding_agent.core.agent import AgentLoop, AgentEvent, RetryConfig
from coding_agent.core.config import AgentConfig
from coding_agent.core.state import AgentState
from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.base import Tool, ToolPermission


class _FlakyTool(Tool):
    """前 N 次返回瞬态错误，之后成功。"""

    def __init__(self, fail_times: int):
        self._fail_times = fail_times
        self._calls = 0

    @property
    def name(self): return "flaky"

    @property
    def description(self): return "fails transiently then succeeds"

    @property
    def parameters(self): return {"type": "object", "properties": {}}

    @property
    def permission(self): return ToolPermission.READ

    async def execute(self, **kwargs):
        self._calls += 1
        if self._calls <= self._fail_times:
            return "Error: connection reset, try again"  # transient
        return "OK"


@pytest.fixture
def agent(tmp_path):
    cfg = AgentConfig(model="m", api_key="k", auto_approve=True,
                      session_db_path=str(tmp_path / "s.db"), max_turns=4)
    reg = ToolRegistry()
    reg.register(_FlakyTool(fail_times=2))
    ag = AgentLoop(config=cfg, tool_registry=reg,
                   retry_config=RetryConfig(max_retries=3, base_delay=0.0))
    return ag


@pytest.mark.asyncio
async def test_retrying_event_surfaced_through_run(agent):
    # 模型：第一轮调用 flaky 工具，第二轮结束
    turns = [
        {"content": "", "tool_calls": [
            {"id": "1", "function": {"name": "flaky", "arguments": "{}"}}]},
        {"content": "done", "tool_calls": []},
    ]
    idx = {"i": 0}

    async def model(ctx, tools):
        r = turns[idx["i"]]; idx["i"] += 1; return r
    agent.set_model_call_fn(model)

    state = AgentState(session_id="t")
    events = []
    async for ev in agent.run(state, "go"):
        events.append(ev)

    retrying = [e for e in events if e.event == AgentEvent.RETRYING]
    assert len(retrying) == 2  # 两次瞬态失败 -> 两次重试事件
    assert retrying[0].data["tool_name"] == "flaky"
    assert retrying[0].data["attempt"] == 1

    # 工具最终成功，结果进入历史
    tool_results = [e for e in events if e.event == AgentEvent.TOOL_RESULT]
    assert any(e.data["result"] == "OK" for e in tool_results)
