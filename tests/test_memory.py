"""
测试项目记忆系统

覆盖：
- ProjectMemoryManager: 初始化、知识保存/搜索、去重、会话摘要
- MemorySaveTool / MemorySearchTool / MemoryReadTool: 工具集成
"""
import json
import os
import tempfile
import time

import pytest

from coding_agent.memory.project import (
    KnowledgeEntry,
    ProjectMemoryManager,
    AGENT_DIR,
)
from coding_agent.tools.memory_ops import (
    MemorySaveTool,
    MemorySearchTool,
    MemoryReadTool,
)
from coding_agent.tools.base import ToolPermission


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def tmp_project(tmp_path):
    """创建临时项目目录"""
    return tmp_path / "my-project"


@pytest.fixture
def manager(tmp_project):
    """已初始化的 ProjectMemoryManager"""
    m = ProjectMemoryManager(tmp_project)
    m.init_project("Test Project")
    return m


# ── KnowledgeEntry ──────────────────────────────────────────────────


class TestKnowledgeEntry:
    def test_create_with_defaults(self):
        entry = KnowledgeEntry(content="Python uses pytest for testing")
        assert entry.content == "Python uses pytest for testing"
        assert entry.tags == []
        assert entry.source == ""
        assert entry.id != ""  # auto-generated
        assert len(entry.id) == 16

    def test_id_is_deterministic(self):
        e1 = KnowledgeEntry(content="same content")
        e2 = KnowledgeEntry(content="same content")
        assert e1.id == e2.id

    def test_id_differs_for_different_content(self):
        e1 = KnowledgeEntry(content="content A")
        e2 = KnowledgeEntry(content="content B")
        assert e1.id != e2.id

    def test_id_case_insensitive_dedup(self):
        """去重忽略大小写"""
        e1 = KnowledgeEntry(content="Hello World")
        e2 = KnowledgeEntry(content="hello world")
        assert e1.id == e2.id

    def test_to_dict_and_from_dict(self):
        entry = KnowledgeEntry(
            content="test",
            tags=["python"],
            source="session-1",
        )
        d = entry.to_dict()
        restored = KnowledgeEntry.from_dict(d)
        assert restored.content == entry.content
        assert restored.tags == entry.tags
        assert restored.source == entry.source
        assert restored.id == entry.id

    def test_matches_query_content(self):
        entry = KnowledgeEntry(content="Use pytest for unit tests")
        assert entry.matches_query("pytest")
        assert entry.matches_query("unit")
        assert not entry.matches_query("java")

    def test_matches_query_tags(self):
        entry = KnowledgeEntry(content="test", tags=["python", "testing"])
        assert entry.matches_query("python")
        assert entry.matches_query("testing")

    def test_matches_tags_all(self):
        entry = KnowledgeEntry(content="x", tags=["a", "b", "c"])
        assert entry.matches_tags(["a", "b"])
        assert entry.matches_tags(["a"])
        assert not entry.matches_tags(["a", "z"])


# ── ProjectMemoryManager ────────────────────────────────────────────


class TestProjectMemoryManager:
    def test_init_project_creates_structure(self, tmp_project):
        m = ProjectMemoryManager(tmp_project)
        assert not m.is_initialized

        m.init_project("My App")

        assert m.is_initialized
        assert (tmp_project / AGENT_DIR).is_dir()
        assert (tmp_project / AGENT_DIR / "PROJECT.md").exists()
        assert (tmp_project / AGENT_DIR / "knowledge.jsonl").exists()
        assert (tmp_project / AGENT_DIR / "sessions").is_dir()

    def test_init_project_preserves_existing_project_md(self, tmp_project):
        """重复初始化不覆盖用户编辑过的 PROJECT.md"""
        m = ProjectMemoryManager(tmp_project)
        m.init_project("First")

        # 手动编辑
        custom = "# Custom Content\n\nMy notes here."
        (tmp_project / AGENT_DIR / "PROJECT.md").write_text(custom)

        # 再次初始化
        m.init_project("Second")

        content = (tmp_project / AGENT_DIR / "PROJECT.md").read_text()
        assert content == custom

    def test_project_md_contains_project_name(self, manager, tmp_project):
        content = (tmp_project / AGENT_DIR / "PROJECT.md").read_text()
        assert "Test Project" in content

    def test_save_and_load_knowledge(self, manager):
        entry = manager.save_knowledge(
            content="Use ruff for linting",
            tags=["python", "lint"],
            source="test-session",
        )
        assert entry.content == "Use ruff for linting"

        loaded = manager.load_knowledge()
        assert len(loaded) == 1
        assert loaded[0].content == "Use ruff for linting"
        assert loaded[0].tags == ["python", "lint"]

    def test_save_knowledge_deduplication(self, manager):
        """相同内容不重复存储"""
        manager.save_knowledge(content="Same content", tags=["a"])
        manager.save_knowledge(content="Same content", tags=["b"])  # 重复

        entries = manager.load_knowledge()
        assert len(entries) == 1
        # 保留第一次的 tags
        assert entries[0].tags == ["a"]

    def test_save_knowledge_case_insensitive_dedup(self, manager):
        """大小写不同视为重复"""
        manager.save_knowledge(content="Hello World")
        manager.save_knowledge(content="hello world")

        entries = manager.load_knowledge()
        assert len(entries) == 1

    def test_search_knowledge_by_query(self, manager):
        manager.save_knowledge(content="Python uses pytest", tags=["python"])
        manager.save_knowledge(content="JavaScript uses Jest", tags=["js"])
        manager.save_knowledge(content="TypeScript also uses Jest", tags=["ts"])

        results = manager.search_knowledge(query="pytest")
        assert len(results) == 1
        assert "pytest" in results[0].content

    def test_search_knowledge_by_tags(self, manager):
        manager.save_knowledge(content="Entry 1", tags=["python", "test"])
        manager.save_knowledge(content="Entry 2", tags=["python", "build"])
        manager.save_knowledge(content="Entry 3", tags=["js"])

        results = manager.search_knowledge(tags=["python"])
        assert len(results) == 2

        results = manager.search_knowledge(tags=["python", "test"])
        assert len(results) == 1
        assert results[0].content == "Entry 1"

    def test_search_knowledge_combined(self, manager):
        manager.save_knowledge(content="pytest config", tags=["python", "test"])
        manager.save_knowledge(content="pytest tips", tags=["python", "tips"])
        manager.save_knowledge(content="jest config", tags=["js", "test"])

        results = manager.search_knowledge(query="pytest", tags=["python"])
        assert len(results) == 2

        results = manager.search_knowledge(query="pytest", tags=["js"])
        assert len(results) == 0

    def test_search_knowledge_limit(self, manager):
        for i in range(20):
            manager.save_knowledge(content=f"Entry {i}", tags=["bulk"])

        results = manager.search_knowledge(tags=["bulk"], limit=5)
        assert len(results) == 5

    def test_search_empty_returns_no_results(self, manager):
        results = manager.search_knowledge(query="anything")
        assert results == []

    def test_save_and_list_session_summaries(self, manager):
        manager.save_session_summary(
            session_id="sess-001",
            summary="Implemented login feature",
            metadata={"files_changed": 3},
        )
        # Small delay to ensure different mtime
        import time as _time
        _time.sleep(0.05)
        manager.save_session_summary(
            session_id="sess-002",
            summary="Fixed auth bug",
        )

        summaries = manager.list_session_summaries()
        assert len(summaries) == 2
        # 最新的在前
        assert summaries[0]["session_id"] == "sess-002"
        assert summaries[1]["session_id"] == "sess-001"

    def test_get_context_for_agent(self, manager):
        context = manager.get_context_for_agent()
        assert "Test Project" in context
        assert "PROJECT.md" in context

    def test_get_context_includes_recent_knowledge(self, manager):
        manager.save_knowledge(content="Key insight about architecture")
        context = manager.get_context_for_agent()
        assert "Key insight about architecture" in context

    def test_read_write_project_md(self, manager):
        new_content = "# Updated\n\nNew content here."
        manager.write_project_md(new_content)
        assert manager.read_project_md() == new_content

    def test_knowledge_jsonl_format(self, manager, tmp_project):
        """验证 JSONL 格式：每行一个合法 JSON"""
        manager.save_knowledge(content="Line 1", tags=["a"])
        manager.save_knowledge(content="Line 2", tags=["b"])

        jsonl_path = tmp_project / AGENT_DIR / "knowledge.jsonl"
        lines = jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 2

        for line in lines:
            data = json.loads(line)  # 不应抛异常
            assert "id" in data
            assert "content" in data
            assert "tags" in data
            assert "timestamp" in data

    def test_load_knowledge_skips_corrupted_lines(self, manager, tmp_project):
        """损坏的 JSONL 行被跳过"""
        jsonl_path = tmp_project / AGENT_DIR / "knowledge.jsonl"
        jsonl_path.write_text(
            '{"id":"good","content":"ok","tags":[],"timestamp":0,"source":""}\n'
            "this is not json\n"
            '{"id":"also_good","content":"also ok","tags":[],"timestamp":0,"source":""}\n'
        )

        entries = manager.load_knowledge()
        assert len(entries) == 2


# ── Tools ───────────────────────────────────────────────────────────


class TestMemoryTools:
    """工具级别测试（通过 execute 调用）"""

    @pytest.fixture
    def tools_in_project(self, tmp_project):
        """在临时项目中创建工具实例"""
        root = str(tmp_project)
        save_tool = MemorySaveTool(get_project_root=lambda: root)
        search_tool = MemorySearchTool(get_project_root=lambda: root)
        read_tool = MemoryReadTool(get_project_root=lambda: root)
        return save_tool, search_tool, read_tool

    @pytest.mark.asyncio
    async def test_memory_save_and_search(self, tools_in_project):
        save_tool, search_tool, read_tool = tools_in_project

        # 保存
        result = await save_tool.execute(
            content="Project uses FastAPI",
            tags=["python", "api"],
        )
        assert "Saved" in result

        # 搜索
        result = await search_tool.execute(query="FastAPI")
        assert "FastAPI" in result
        assert "python" in result

    @pytest.mark.asyncio
    async def test_memory_save_requires_content(self, tools_in_project):
        save_tool, _, _ = tools_in_project

        with pytest.raises(Exception):
            await save_tool.execute()

    @pytest.mark.asyncio
    async def test_memory_search_no_results(self, tools_in_project):
        save_tool, search_tool, _ = tools_in_project

        # 初始化项目以便测试"无结果"而非"未初始化"
        await save_tool.execute(content="seed")

        result = await search_tool.execute(query="nonexistent")
        assert "No knowledge found" in result

    @pytest.mark.asyncio
    async def test_memory_read_empty(self, tools_in_project):
        _, _, read_tool = tools_in_project

        result = await read_tool.execute()
        # 尚未初始化时应返回提示
        assert "No project memory" in result or "PROJECT.md" in result

    @pytest.mark.asyncio
    async def test_memory_read_after_save(self, tools_in_project):
        save_tool, _, read_tool = tools_in_project

        # 保存会触发自动初始化
        await save_tool.execute(content="important fact")

        result = await read_tool.execute()
        assert "important fact" in result

    @pytest.mark.asyncio
    async def test_memory_save_auto_inits_project(self, tmp_project):
        """保存时自动初始化 .agent/ 目录"""
        root = str(tmp_project)
        save_tool = MemorySaveTool(get_project_root=lambda: root)

        assert not (tmp_project / AGENT_DIR).exists()

        await save_tool.execute(content="first knowledge")

        assert (tmp_project / AGENT_DIR).is_dir()
        assert (tmp_project / AGENT_DIR / "PROJECT.md").exists()

    @pytest.mark.asyncio
    async def test_memory_save_dedup(self, tools_in_project):
        save_tool, search_tool, _ = tools_in_project

        await save_tool.execute(content="duplicate content")
        await save_tool.execute(content="duplicate content")

        result = await search_tool.execute(query="duplicate")
        # 应该只有一条
        lines = [l for l in result.strip().split("\n") if l.startswith("- ")]
        assert len(lines) == 1

    @pytest.mark.asyncio
    async def test_memory_search_by_tags(self, tools_in_project):
        save_tool, search_tool, _ = tools_in_project

        await save_tool.execute(content="entry A", tags=["python", "fast"])
        await save_tool.execute(content="entry B", tags=["js", "fast"])
        await save_tool.execute(content="entry C", tags=["python", "slow"])

        result = await search_tool.execute(tags=["python"])
        assert "entry A" in result
        assert "entry C" in result
        assert "entry B" not in result

    @pytest.mark.asyncio
    async def test_tool_definitions(self, tools_in_project):
        save_tool, search_tool, read_tool = tools_in_project

        assert save_tool.name == "memory_save"
        assert save_tool.permission == ToolPermission.WRITE

        assert search_tool.name == "memory_search"
        assert search_tool.permission == ToolPermission.READ

        assert read_tool.name == "memory_read"
        assert read_tool.permission == ToolPermission.READ

    @pytest.mark.asyncio
    async def test_openai_function_format(self, tools_in_project):
        save_tool, search_tool, _ = tools_in_project

        fmt = save_tool.get_openai_function()
        assert fmt["name"] == "memory_save"
        assert "content" in fmt["parameters"]["properties"]
        assert "tags" in fmt["parameters"]["properties"]

        fmt = search_tool.get_openai_function()
        assert fmt["name"] == "memory_search"
        assert "query" in fmt["parameters"]["properties"]
        assert "tags" in fmt["parameters"]["properties"]


# ── Registration ────────────────────────────────────────────────────


class TestRegistration:
    def test_register_memory_tools(self):
        """注册后工具可从 registry 获取"""
        from coding_agent.tools.registry import ToolRegistry
        from coding_agent.tools.memory_ops import register_memory_tools

        reg = ToolRegistry()
        # 临时替换全局 registry
        import coding_agent.tools.registry as reg_mod
        old = reg_mod._global_registry
        reg_mod._global_registry = reg

        try:
            register_memory_tools()

            tool_names = [t.name for t in reg.get_all_tools()]
            assert "memory_save" in tool_names
            assert "memory_search" in tool_names
            assert "memory_read" in tool_names
        finally:
            reg_mod._global_registry = old
