"""
测试计划工具 update_plan
"""
import pytest

from coding_agent.tools.plan_ops import (
    UpdatePlanTool,
    register_plan_tools,
    render_plan,
    PLAN_METADATA_KEY,
)
from coding_agent.tools.registry import ToolRegistry
from coding_agent.core.state import AgentState


@pytest.mark.asyncio
async def test_update_plan_persists_to_state():
    state = AgentState()
    tool = UpdatePlanTool(state=state)
    out = await tool.execute(steps=[
        {"step": "Read the code", "status": "completed"},
        {"step": "Write the fix", "status": "in_progress"},
        {"step": "Run tests", "status": "pending"},
    ])
    assert "1/3 completed" in out
    assert "[x] Read the code" in out
    assert "[~] Write the fix" in out
    plan = state.metadata[PLAN_METADATA_KEY]
    assert len(plan) == 3
    assert plan[1]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_rejects_multiple_in_progress():
    tool = UpdatePlanTool(state=AgentState())
    out = await tool.execute(steps=[
        {"step": "A", "status": "in_progress"},
        {"step": "B", "status": "in_progress"},
    ])
    assert out.startswith("Error")
    assert "in_progress" in out


@pytest.mark.asyncio
async def test_rejects_invalid_status():
    tool = UpdatePlanTool(state=AgentState())
    out = await tool.execute(steps=[{"step": "A", "status": "doing"}])
    assert out.startswith("Error")


@pytest.mark.asyncio
async def test_rejects_empty_steps():
    tool = UpdatePlanTool(state=AgentState())
    out = await tool.execute(steps=[])
    assert out.startswith("Error")


@pytest.mark.asyncio
async def test_register_and_bind_state():
    reg = ToolRegistry()
    tool = register_plan_tools(registry=reg)
    assert reg.get_tool("update_plan") is tool
    assert tool.permission.value == "read"

    state = AgentState()
    tool.bind_state(state)
    await tool.execute(steps=[{"step": "X", "status": "pending"}])
    assert state.metadata[PLAN_METADATA_KEY][0]["step"] == "X"


def test_render_plan_symbols():
    out = render_plan([
        {"step": "a", "status": "completed"},
        {"step": "b", "status": "pending"},
    ])
    assert "[x] a" in out
    assert "[ ] b" in out
    assert "1/2 completed" in out
