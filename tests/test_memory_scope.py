"""
测试 memory 作用域分层（project + global，参考 Codex）。
"""
import asyncio

from coding_agent.tools.memory_ops import (
    MemorySaveTool, MemorySearchTool, global_memory_root,
)


def _tools(tmp_path, monkeypatch):
    # 项目根 = tmp_path/proj；全局根 = tmp_path/home 下
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "cfg"))
    save = MemorySaveTool(get_project_root=lambda: str(proj))
    search = MemorySearchTool(get_project_root=lambda: str(proj))
    return save, search, proj


def test_global_root_uses_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "cfg"))
    assert global_memory_root() == str(tmp_path / "cfg" / "memory")


def test_save_project_scope(tmp_path, monkeypatch):
    save, search, proj = _tools(tmp_path, monkeypatch)
    out = asyncio.run(save.execute(content="build with make", scope="project"))
    assert "project memory" in out
    # 存到项目 .agent/ 下
    assert (proj / ".agent" / "knowledge.jsonl").exists()


def test_save_global_scope(tmp_path, monkeypatch):
    save, search, proj = _tools(tmp_path, monkeypatch)
    out = asyncio.run(save.execute(content="I prefer tabs", scope="global"))
    assert "global memory" in out
    # 存到全局目录，不在项目里
    assert (tmp_path / "cfg" / "memory" / ".agent" / "knowledge.jsonl").exists()
    assert not (proj / ".agent" / "knowledge.jsonl").exists()


def test_search_spans_both_scopes(tmp_path, monkeypatch):
    save, search, proj = _tools(tmp_path, monkeypatch)
    asyncio.run(save.execute(content="project fact alpha", scope="project", tags=["x"]))
    asyncio.run(save.execute(content="global fact beta", scope="global", tags=["x"]))
    out = asyncio.run(search.execute(query="fact", scope="all"))
    assert "project fact alpha" in out and "(project)" in out
    assert "global fact beta" in out and "(global)" in out


def test_search_project_only(tmp_path, monkeypatch):
    save, search, proj = _tools(tmp_path, monkeypatch)
    asyncio.run(save.execute(content="project fact alpha", scope="project"))
    asyncio.run(save.execute(content="global fact beta", scope="global"))
    out = asyncio.run(search.execute(query="fact", scope="project"))
    assert "project fact alpha" in out
    assert "global fact beta" not in out


def test_search_global_only(tmp_path, monkeypatch):
    save, search, proj = _tools(tmp_path, monkeypatch)
    asyncio.run(save.execute(content="project fact alpha", scope="project"))
    asyncio.run(save.execute(content="global fact beta", scope="global"))
    out = asyncio.run(search.execute(query="fact", scope="global"))
    assert "global fact beta" in out
    assert "project fact alpha" not in out


def test_default_scope_is_project(tmp_path, monkeypatch):
    save, search, proj = _tools(tmp_path, monkeypatch)
    out = asyncio.run(save.execute(content="default goes to project"))
    assert "project memory" in out
    assert (proj / ".agent" / "knowledge.jsonl").exists()
