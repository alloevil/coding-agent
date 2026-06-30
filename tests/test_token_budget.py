"""
测试 token 预算停止
"""
import pytest

from coding_agent.core.agent import AgentLoop, AgentEvent
from coding_agent.core.config import AgentConfig
from coding_agent.core.state import AgentState
from coding_agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_stops_when_token_budget_exceeded(tmp_path):
    cfg = AgentConfig(model="m", api_key="k", auto_approve=True,
                      session_db_path=str(tmp_path / "s.db"), max_turns=50,
                      max_total_tokens=100)
    agent = AgentLoop(config=cfg, tool_registry=ToolRegistry())

    # 模型每轮都让它继续（无 tool_calls 会自然 DONE，这里用一个永远要工具的 mock）
    calls = {"n": 0}
    async def model(ctx, tools):
        calls["n"] += 1
        return {"content": "thinking...", "tool_calls": []}
    agent.set_model_call_fn(model)

    # 模拟累计 token：第一次调用后就超预算
    tokens = {"v": 0}
    def usage():
        return tokens["v"]
    agent.set_token_usage_fn(usage)

    # 单轮就会 DONE（无 tool_calls），所以改造：让 token 在进入第 2 轮前超标
    # 这里直接验证预算门：预算=100，usage 返回 150 -> 第一轮就拦截
    tokens["v"] = 150
    state = AgentState(session_id="t")
    events = []
    async for ev in agent.run(state, "go"):
        events.append(ev)
    done = [e for e in events if e.event == AgentEvent.DONE]
    assert done and done[0].data.get("reason") == "token_budget_exceeded"
    assert calls["n"] == 0  # 预算在调模型前就拦截了


@pytest.mark.asyncio
async def test_no_budget_means_no_limit(tmp_path):
    cfg = AgentConfig(model="m", api_key="k", auto_approve=True,
                      session_db_path=str(tmp_path / "s.db"), max_turns=3,
                      max_total_tokens=0)  # 0 = 不限制
    agent = AgentLoop(config=cfg, tool_registry=ToolRegistry())
    async def model(ctx, tools):
        return {"content": "done", "tool_calls": []}
    agent.set_model_call_fn(model)
    agent.set_token_usage_fn(lambda: 10_000_000)  # 巨大，但预算=0 不该拦
    state = AgentState(session_id="t")
    events = [ev async for ev in agent.run(state, "go")]
    done = [e for e in events if e.event == AgentEvent.DONE]
    assert done and done[0].data.get("reason") != "token_budget_exceeded"
