"""
测试 skills 注入到 context（渐进式披露的"清单"半步）。

验证 ContextManager 的 extra_system_provider 钩子把可用 skills 清单作为
system 块注入，且为空时不注入。
"""
from coding_agent.context.manager import ContextManager
from coding_agent.core.state import AgentState


def _system_contents(messages):
    return [m["content"] for m in messages if m["role"] == "system"]


def test_extra_provider_injected():
    cm = ContextManager(load_project_context=False,
                        extra_system_provider=lambda: "<available_skills>x</available_skills>")
    state = AgentState(session_id="t")
    msgs = cm.assemble_context(state, "SYS")
    joined = "\n".join(_system_contents(msgs))
    assert "<available_skills>" in joined


def test_no_provider_no_injection():
    cm = ContextManager(load_project_context=False)
    state = AgentState(session_id="t")
    msgs = cm.assemble_context(state, "SYS")
    # 只有系统提示词一条 system
    assert _system_contents(msgs) == ["SYS"]


def test_empty_provider_skipped():
    cm = ContextManager(load_project_context=False,
                        extra_system_provider=lambda: "")
    state = AgentState(session_id="t")
    msgs = cm.assemble_context(state, "SYS")
    assert _system_contents(msgs) == ["SYS"]


def test_provider_exception_swallowed():
    def boom():
        raise RuntimeError("nope")
    cm = ContextManager(load_project_context=False, extra_system_provider=boom)
    state = AgentState(session_id="t")
    # 不应抛出
    msgs = cm.assemble_context(state, "SYS")
    assert _system_contents(msgs) == ["SYS"]


def test_setter_on_agent_loop(tmp_path):
    from coding_agent.core.agent import AgentLoop
    from coding_agent.core.config import AgentConfig
    cfg = AgentConfig(model="m", api_key="k",
                      session_db_path=str(tmp_path / "s.db"))
    loop = AgentLoop(config=cfg)
    loop.set_extra_system_provider(lambda: "INJECTED")
    state = AgentState(session_id="t")
    msgs = loop.context_manager.assemble_context(state, "SYS")
    assert "INJECTED" in "\n".join(_system_contents(msgs))
