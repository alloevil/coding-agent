"""
测试 agent profile 在 spawn / 命令 / 工具过滤里的接线。
"""
import asyncio

import pytest

from coding_agent.core.commands import dispatch, CommandContext
from coding_agent.core.agent import AgentLoop, _tool_fn_name
from coding_agent.core.config import AgentConfig
from coding_agent.core.state import AgentState


def _make_agent_file(tmp_path, name, frontmatter, body="You are X."):
    d = tmp_path / ".coding-agent" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    fm = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
    (d / f"{name}.md").write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")


# ---- 命令 ----

def test_agents_command_lists(tmp_path, monkeypatch):
    _make_agent_file(tmp_path, "reviewer", {"description": "Reviews"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    r = dispatch("/agents", CommandContext(tool_names=[]))
    assert r.kind == "print"
    assert "reviewer" in r.payload


def test_agent_switch_returns_action():
    r = dispatch("/agent reviewer", CommandContext(tool_names=[]))
    assert r.kind == "action"
    assert r.payload == "agent:reviewer"


def test_agent_no_arg_shows_usage():
    r = dispatch("/agent", CommandContext(tool_names=[]))
    assert r.kind == "print"
    assert "Usage" in r.payload


# ---- 工具过滤 ----

def test_tool_fn_name_helper():
    assert _tool_fn_name({"type": "function", "function": {"name": "grep"}}) == "grep"
    assert _tool_fn_name({"name": "x"}) == "x"


def test_tool_filter_blocks_at_dispatch(tmp_path):
    cfg = AgentConfig(model="m", api_key="k", auto_approve=True,
                      session_db_path=str(tmp_path / "s.db"), max_turns=2)
    from coding_agent.tools.registry import ToolRegistry
    from coding_agent.tools.file_ops import register_file_tools
    reg = ToolRegistry()
    register_file_tools(reg)
    loop = AgentLoop(config=cfg, tool_registry=reg)
    # 只允许读类工具
    loop.set_tool_filter(lambda name: name in ("file_read", "grep"))

    from coding_agent.core.agent import AgentEvent
    from coding_agent.core.state import ToolCall

    target = tmp_path / "out.txt"
    turns = [
        {"content": "", "tool_calls": [{"id": "1", "function": {
            "name": "file_write",
            "arguments": '{"path": "%s", "content": "x"}' % target}}]},
        {"content": "done", "tool_calls": []},
    ]
    idx = {"i": 0}
    async def model(ctx, tools):
        # 被过滤的工具不应出现在传给模型的工具列表里
        names = [_tool_fn_name(t) for t in tools]
        assert "file_write" not in names
        assert "file_read" in names
        r = turns[idx["i"]]; idx["i"] += 1; return r
    loop.set_model_call_fn(model)

    state = AgentState(session_id="t")
    errored = False
    async def run():
        nonlocal errored
        async for ev in loop.run(state, "go"):
            if ev.event == AgentEvent.TOOL_RESULT and ev.data["is_error"]:
                errored = True
    asyncio.run(run())
    assert errored                # 被禁工具调用被拒
    assert not target.exists()    # 没真写


# ---- spawn with profile ----

def test_spawn_unknown_profile_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    from coding_agent.tools.agent_ops import _run_subagent

    class _FakeParent:
        config = AgentConfig(model="m", api_key="k",
                             session_db_path=str(tmp_path / "s.db"))
        _spawn_depth = 0
        tool_registry = None
        _model_call_fn = None

    out = asyncio.run(_run_subagent(
        parent_agent=_FakeParent(), task="t", profile_name="ghost"))
    assert "not found" in out.lower()
