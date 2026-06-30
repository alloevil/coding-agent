"""
测试只读规划模式 plan_mode
"""
import pytest

from coding_agent.core.permissions import PermissionPolicy, Decision
from coding_agent.tools.base import ToolPermission as TP


def test_plan_mode_allows_read():
    p = PermissionPolicy(plan_mode=True)
    assert p.decide("file_read", {"path": "a.py"}, TP.READ) == Decision.ALLOW
    assert p.decide("grep", {"pattern": "x"}, TP.READ) == Decision.ALLOW


def test_plan_mode_denies_write_and_exec():
    p = PermissionPolicy(plan_mode=True)
    assert p.decide("file_write", {"path": "a.py"}, TP.WRITE) == Decision.DENY
    assert p.decide("shell_exec", {"command": "ls"}, TP.EXECUTE) == Decision.DENY
    assert p.decide("apply_patch", {"patch": "x"}, TP.WRITE) == Decision.DENY


def test_plan_mode_allows_update_plan_and_ask():
    p = PermissionPolicy(plan_mode=True)
    assert p.decide("update_plan", {}, TP.READ) == Decision.ALLOW
    assert p.decide("ask_user", {}, TP.READ) == Decision.ALLOW


def test_plan_mode_overrides_auto_approve():
    # 即使 auto_approve，plan_mode 仍拦写操作
    p = PermissionPolicy(plan_mode=True, auto_approve=True)
    assert p.decide("file_write", {"path": "x"}, TP.WRITE) == Decision.DENY


def test_plan_mode_off_normal():
    p = PermissionPolicy(plan_mode=False, auto_approve=True)
    assert p.decide("file_write", {"path": "x"}, TP.WRITE) == Decision.ALLOW


def test_plan_mode_secret_read_still_denied():
    p = PermissionPolicy(plan_mode=True)
    assert p.decide("file_read", {"path": "config/.env"}, TP.READ) == Decision.DENY


def test_plan_mode_command_returns_action():
    from coding_agent.core.commands import dispatch, CommandContext
    r = dispatch("/plan-mode", CommandContext(tool_names=[]))
    assert r.kind == "action" and r.payload == "plan_mode"


@pytest.mark.asyncio
async def test_plan_mode_blocks_write_in_run(tmp_path):
    from coding_agent.core.agent import AgentLoop, AgentEvent
    from coding_agent.core.config import AgentConfig
    from coding_agent.core.state import AgentState
    from coding_agent.tools.registry import ToolRegistry
    from coding_agent.tools.file_ops import register_file_tools

    cfg = AgentConfig(model="m", api_key="k", auto_approve=True,
                      session_db_path=str(tmp_path / "s.db"), max_turns=3)
    reg = ToolRegistry()
    register_file_tools(reg)
    agent = AgentLoop(config=cfg, tool_registry=reg)
    agent.permission_policy.plan_mode = True

    target = tmp_path / "out.txt"
    turns = [
        {"content": "", "tool_calls": [{"id": "1", "function": {
            "name": "file_write",
            "arguments": '{"path": "%s", "content": "x"}' % target}}]},
        {"content": "done", "tool_calls": []},
    ]
    idx = {"i": 0}
    async def model(ctx, tools):
        r = turns[idx["i"]]; idx["i"] += 1; return r
    agent.set_model_call_fn(model)

    state = AgentState(session_id="t")
    errored = False
    async for ev in agent.run(state, "go"):
        if ev.event == AgentEvent.TOOL_RESULT and ev.data["is_error"]:
            errored = True
    assert errored
    assert not target.exists()  # plan mode 阻止了写入
