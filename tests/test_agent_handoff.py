"""
测试 plan→build 切换交接提醒（合成提醒 + 一次性注入消费）。
"""
from coding_agent.core.agent_handoff import (
    build_switch_note, should_handoff,
)
from coding_agent.context.manager import ContextManager
from coding_agent.core.state import AgentState


def test_build_switch_note_basic():
    note = build_switch_note(had_plan=False)
    assert "execution" in note.lower()
    assert "plan already exists" not in note.lower()


def test_build_switch_note_with_plan():
    note = build_switch_note(had_plan=True)
    assert "plan already exists" in note.lower()


def test_should_handoff_plan_agent_to_build():
    assert should_handoff("plan", False, "build", False) is True


def test_should_handoff_from_plan_mode():
    # 上一刻在 plan_mode，现在不在 → 交接
    assert should_handoff(None, True, None, False) is True


def test_no_handoff_staying_in_plan():
    assert should_handoff("plan", True, "plan", True) is False


def test_no_handoff_build_to_build():
    assert should_handoff("build", False, "reviewer", False) is False


def test_no_handoff_into_plan():
    # 切换 *进入* plan agent 不应交接
    assert should_handoff("build", False, "plan", True) is False


# ---- 一次性注入 ----

def _system_contents(messages):
    return [m["content"] for m in messages if m["role"] == "system"]


def test_pending_handoff_injected_once():
    cm = ContextManager(load_project_context=False)
    state = AgentState(session_id="t")
    state.metadata["pending_handoff"] = "HANDOFF NOTE"
    msgs = cm.assemble_context(state, "SYS")
    assert "HANDOFF NOTE" in "\n".join(_system_contents(msgs))
    # 已消费：再次组装不应再出现
    msgs2 = cm.assemble_context(state, "SYS")
    assert "HANDOFF NOTE" not in "\n".join(_system_contents(msgs2))
    assert "pending_handoff" not in state.metadata


def test_no_handoff_no_injection():
    cm = ContextManager(load_project_context=False)
    state = AgentState(session_id="t")
    msgs = cm.assemble_context(state, "SYS")
    assert _system_contents(msgs) == ["SYS"]
