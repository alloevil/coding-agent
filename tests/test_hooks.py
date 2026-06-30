"""
测试生命周期 hook 触发 + 配置驱动的命令 hook
"""
import pytest

from coding_agent.core.agent import AgentLoop, AgentEvent
from coding_agent.core.config import AgentConfig
from coding_agent.core.state import AgentState
from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.base import HookEvent, HookContext
from coding_agent.core.hooks_config import register_config_hooks


@pytest.mark.asyncio
async def test_model_call_hooks_fire(tmp_path):
    cfg = AgentConfig(model="m", api_key="k", auto_approve=True,
                      session_db_path=str(tmp_path / "s.db"), max_turns=2)
    reg = ToolRegistry()
    fired = []
    reg.add_hook(HookEvent.PRE_MODEL_CALL, lambda ctx: fired.append("pre"))
    reg.add_hook(HookEvent.POST_MODEL_CALL, lambda ctx: fired.append("post"))
    agent = AgentLoop(config=cfg, tool_registry=reg)
    async def model(ctx, tools):
        return {"content": "done", "tool_calls": []}
    agent.set_model_call_fn(model)
    state = AgentState(session_id="t")
    async for _ in agent.run(state, "go"):
        pass
    assert "pre" in fired and "post" in fired


def test_register_config_hooks_counts():
    reg = ToolRegistry()
    n = register_config_hooks({
        "pre_tool_use": [{"command": "true"}],
        "post_tool_use": ["echo hi"],   # 字符串简写
        "bogus_event": [{"command": "x"}],  # 未知事件忽略
    }, reg)
    assert n == 2


@pytest.mark.asyncio
async def test_command_hook_runs_and_can_block():
    reg = ToolRegistry()
    # block=True 且命令返回非零 -> 阻断
    register_config_hooks({"pre_tool_use": [{"command": "exit 1", "block": True}]}, reg)
    blocked = await reg.run_hooks(
        HookEvent.PRE_TOOL_USE, HookContext(event=HookEvent.PRE_TOOL_USE, tool_name="x"))
    assert blocked is True


@pytest.mark.asyncio
async def test_command_hook_nonblocking_default():
    reg = ToolRegistry()
    register_config_hooks({"post_tool_use": [{"command": "exit 1"}]}, reg)  # 默认不阻断
    blocked = await reg.run_hooks(
        HookEvent.POST_TOOL_USE, HookContext(event=HookEvent.POST_TOOL_USE))
    assert blocked is False
