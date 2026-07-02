"""
测试记忆自动召回：保存的项目知识会通过 extra_system_provider 进入组装的上下文，
无需模型主动调 memory_search。
"""
from coding_agent.memory.project import ProjectMemoryManager
from coding_agent.context.manager import ContextManager
from coding_agent.core.state import AgentState


def test_saved_knowledge_appears_in_context(tmp_path):
    # 在临时项目里存一条知识
    mgr = ProjectMemoryManager(str(tmp_path))
    mgr.save_knowledge("The build uses Bazel, not Make.", tags=["build"])

    # provider 就是 CLI/protocol 里用的那种（这里直接用 manager 的渲染）
    cm = ContextManager(max_tokens=100000, load_project_context=False,
                        extra_system_provider=lambda: mgr.get_context_for_agent())
    st = AgentState()
    st.add_user_message("how do I build this?")
    ctx = cm.assemble_context(st, system_prompt="sys")

    joined = "\n".join(m.get("content", "") for m in ctx if isinstance(m.get("content"), str))
    assert "Bazel" in joined, "saved knowledge should be auto-injected into context"


def test_empty_memory_adds_nothing(tmp_path):
    mgr = ProjectMemoryManager(str(tmp_path))
    cm = ContextManager(max_tokens=100000, load_project_context=False,
                        extra_system_provider=lambda: mgr.get_context_for_agent())
    st = AgentState()
    st.add_user_message("hi")
    ctx = cm.assemble_context(st, system_prompt="sys")
    # 没记忆时不应插入空的 system 块
    system_blocks = [m for m in ctx if m["role"] == "system"]
    # 只有 system_prompt 一个（extra 为空被跳过）
    assert all(b["content"] for b in system_blocks), "no empty system blocks"


def test_get_context_for_agent_renders_recent_knowledge(tmp_path):
    mgr = ProjectMemoryManager(str(tmp_path))
    for i in range(3):
        mgr.save_knowledge(f"fact number {i}", tags=["t"])
    out = mgr.get_context_for_agent()
    assert "fact number 0" in out and "fact number 2" in out
    assert "Recent Project Knowledge" in out


def test_context_caps_long_project_md(tmp_path):
    mgr = ProjectMemoryManager(str(tmp_path))
    mgr.init_project("demo")
    mgr.write_project_md("A" * 10000)  # 10KB
    out = mgr.get_context_for_agent()
    assert "PROJECT.md truncated" in out
    assert len(out) < 6000, "injected block is bounded even for a huge PROJECT.md"


def test_context_caps_long_knowledge_entry(tmp_path):
    mgr = ProjectMemoryManager(str(tmp_path))
    mgr.save_knowledge("X" * 3000, tags=["big"])  # one very long entry
    out = mgr.get_context_for_agent()
    # entry truncated to ~500 chars (+ ellipsis + tag), not the full 3000
    assert "X" * 501 not in out
    assert "…" in out
