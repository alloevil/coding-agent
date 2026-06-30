"""
测试细粒度权限策略 PermissionPolicy
"""
import pytest

from coding_agent.core.permissions import PermissionPolicy, Rule, Decision
from coding_agent.tools.base import ToolPermission as TP


def test_default_read_allowed_write_asks():
    p = PermissionPolicy()
    assert p.decide("file_read", {"path": "a.py"}, TP.READ) == Decision.ALLOW
    assert p.decide("file_write", {"path": "a.py"}, TP.WRITE) == Decision.ASK


def test_auto_approve_allows_write():
    p = PermissionPolicy(auto_approve=True)
    assert p.decide("file_write", {"path": "a.py"}, TP.WRITE) == Decision.ALLOW


def test_deny_beats_allow_and_auto_approve():
    p = PermissionPolicy(
        auto_approve=True,
        allow_rules=[Rule(tool="shell_exec")],
        deny_rules=[Rule(tool="shell_exec", commands=[r"rm\s+-rf"])],
    )
    assert p.decide("shell_exec", {"command": "ls"}, TP.EXECUTE) == Decision.ALLOW
    assert p.decide("shell_exec", {"command": "rm -rf /"}, TP.EXECUTE) == Decision.DENY


def test_builtin_secret_read_denied():
    p = PermissionPolicy()
    assert p.decide("file_read", {"path": "config/.env"}, TP.READ) == Decision.DENY
    assert p.decide("file_read", {"path": "/home/u/.ssh/id_rsa"}, TP.READ) == Decision.DENY
    assert p.decide("file_read", {"path": "src/app.py"}, TP.READ) == Decision.ALLOW


def test_allow_rule_auto_approves_specific_tool():
    p = PermissionPolicy(allow_rules=[Rule(tool="git_commit")])
    assert p.decide("git_commit", {}, TP.WRITE) == Decision.ALLOW
    # 未被 allow 的写操作仍需询问
    assert p.decide("file_write", {"path": "x"}, TP.WRITE) == Decision.ASK


def test_path_scoped_deny():
    p = PermissionPolicy(deny_rules=[Rule(tool="file_write", paths=["/etc/**"])])
    assert p.decide("file_write", {"path": "/etc/passwd"}, TP.WRITE) == Decision.DENY
    assert p.decide("file_write", {"path": "src/x.py"}, TP.WRITE) == Decision.ASK


def test_from_config_shorthand_and_dict():
    p = PermissionPolicy.from_config({
        "allow": ["git_commit", {"tool": "shell_exec", "commands": ["pytest"]}],
        "deny": [{"tool": "shell_exec", "commands": [r"sudo"]}],
    })
    assert p.decide("git_commit", {}, TP.WRITE) == Decision.ALLOW
    assert p.decide("shell_exec", {"command": "pytest -q"}, TP.EXECUTE) == Decision.ALLOW
    assert p.decide("shell_exec", {"command": "sudo rm"}, TP.EXECUTE) == Decision.DENY


@pytest.mark.asyncio
async def test_policy_deny_blocks_execution_in_run(tmp_path):
    """deny 规则应在 run() 中阻止工具执行，不实际写文件。"""
    from coding_agent.core.agent import AgentLoop, AgentEvent
    from coding_agent.core.config import AgentConfig
    from coding_agent.core.state import AgentState
    from coding_agent.tools.registry import ToolRegistry
    from coding_agent.tools.file_ops import register_file_tools

    cfg = AgentConfig(model="m", api_key="k", auto_approve=True,
                      session_db_path=str(tmp_path / "s.db"), max_turns=4,
                      permissions={"deny": [{"tool": "file_write", "paths": ["**/secret.txt"]}]})
    reg = ToolRegistry()
    register_file_tools(reg)
    agent = AgentLoop(config=cfg, tool_registry=reg)

    target = tmp_path / "secret.txt"
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
    assert not target.exists(), "deny policy should have blocked the write"
