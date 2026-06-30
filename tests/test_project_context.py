"""
测试项目上下文加载 (AGENTS.md / CLAUDE.md)
"""
import os
import subprocess
import pytest

from coding_agent.context.project_context import (
    discover_context_files,
    load_project_context,
)
from coding_agent.context.manager import ContextManager
from coding_agent.core.state import AgentState


def _init_git(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_no_context_files_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nonexistent"))
    assert load_project_context() == ""
    assert discover_context_files() == []


def test_loads_agents_md_at_repo_root(tmp_path, monkeypatch):
    _init_git(tmp_path)
    (tmp_path / "AGENTS.md").write_text("Use 4-space indent.", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nonexistent"))

    ctx = load_project_context()
    assert "Use 4-space indent." in ctx
    assert "Project instructions" in ctx


def test_hierarchy_order_root_then_subdir(tmp_path, monkeypatch):
    """更深层（cwd）的指令应排在仓库根之后（优先级更高）。"""
    _init_git(tmp_path)
    (tmp_path / "AGENTS.md").write_text("ROOT_RULE", encoding="utf-8")
    sub = tmp_path / "service"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("SUBDIR_RULE", encoding="utf-8")

    monkeypatch.chdir(sub)
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nonexistent"))

    files = discover_context_files()
    names = [str(f) for f in files]
    # 根在前，子目录在后
    root_idx = next(i for i, n in enumerate(names) if n.endswith("service/AGENTS.md") is False and "AGENTS.md" in n)
    sub_idx = next(i for i, n in enumerate(names) if n.endswith("service/AGENTS.md"))
    assert root_idx < sub_idx

    ctx = load_project_context()
    assert ctx.index("ROOT_RULE") < ctx.index("SUBDIR_RULE")


def test_both_agents_and_claude_loaded(tmp_path, monkeypatch):
    _init_git(tmp_path)
    (tmp_path / "AGENTS.md").write_text("FROM_AGENTS", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("FROM_CLAUDE", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nonexistent"))

    ctx = load_project_context()
    assert "FROM_AGENTS" in ctx
    assert "FROM_CLAUDE" in ctx
    # AGENTS.md 在 CLAUDE.md 之前
    assert ctx.index("FROM_AGENTS") < ctx.index("FROM_CLAUDE")


def test_context_manager_injects_project_context(tmp_path, monkeypatch):
    _init_git(tmp_path)
    (tmp_path / "AGENTS.md").write_text("PROJECT_CONVENTION", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nonexistent"))

    cm = ContextManager(max_tokens=1000)
    state = AgentState()
    state.add_user_message("hello")
    messages = cm.assemble_context(state, "SYSTEM")

    assert messages[0] == {"role": "system", "content": "SYSTEM"}
    assert messages[1]["role"] == "system"
    assert "PROJECT_CONVENTION" in messages[1]["content"]
    assert messages[2]["content"] == "hello"


def test_context_manager_can_disable_project_context(tmp_path, monkeypatch):
    _init_git(tmp_path)
    (tmp_path / "AGENTS.md").write_text("SHOULD_NOT_APPEAR", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cm = ContextManager(max_tokens=1000, load_project_context=False)
    state = AgentState()
    state.add_user_message("hi")
    messages = cm.assemble_context(state, "SYSTEM")
    joined = " ".join(str(m) for m in messages)
    assert "SHOULD_NOT_APPEAR" not in joined


# ── nested AGENTS.md from touched dirs ──────────────────────────────────────
from coding_agent.context import project_context as pc


def test_nested_agents_md_surfaced_after_read(tmp_path, monkeypatch):
    pc._TOUCHED_DIRS.clear()
    _init_git(tmp_path)
    (tmp_path / "AGENTS.md").write_text("ROOT_RULE", encoding="utf-8")
    sub = tmp_path / "service"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("SERVICE_RULE", encoding="utf-8")
    (sub / "app.py").write_text("x=1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nope"))

    # 起始只在 root：不含 service 规则
    ctx0 = pc.load_project_context()
    assert "ROOT_RULE" in ctx0
    assert "SERVICE_RULE" not in ctx0

    # agent 读了 service/app.py -> 登记该目录
    pc.note_touched_path(str(sub / "app.py"))
    ctx1 = pc.load_project_context()
    assert "SERVICE_RULE" in ctx1
    pc._TOUCHED_DIRS.clear()


def test_context_manager_refreshes_on_new_touched_dir(tmp_path, monkeypatch):
    pc._TOUCHED_DIRS.clear()
    _init_git(tmp_path)
    (tmp_path / "AGENTS.md").write_text("ROOT", encoding="utf-8")
    sub = tmp_path / "pkg"; sub.mkdir()
    (sub / "CLAUDE.md").write_text("PKG_RULE", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nope"))

    cm = ContextManager(max_tokens=10000)
    state = AgentState(); state.add_user_message("hi")
    msgs0 = cm.assemble_context(state, "SYS")
    assert not any("PKG_RULE" in str(m) for m in msgs0)

    pc.note_touched_path(str(sub / "x.py"))
    msgs1 = cm.assemble_context(state, "SYS")
    assert any("PKG_RULE" in str(m) for m in msgs1)  # 缓存已失效并纳入
    pc._TOUCHED_DIRS.clear()
