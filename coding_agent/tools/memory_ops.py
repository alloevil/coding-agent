"""
项目记忆工具 - memory_save / memory_search

注册到工具系统，让 Agent 可以跨会话保存和检索项目知识。

工具权限：
- memory_save: WRITE
- memory_search: READ
"""
from __future__ import annotations

import os
from typing import Any

from .base import Tool, ToolPermission, ToolExecutionError
from ..memory.project import ProjectMemoryManager


class MemorySaveTool(Tool):
    """
    保存知识到项目记忆

    将当前对话中的重要发现保存到 .agent/knowledge.jsonl。
    自动去重：相似内容不重复存储。
    """

    def __init__(self, get_project_root: Any = None) -> None:
        """
        Args:
            get_project_root: 可调用对象，返回当前项目根目录。
                              如果为 None，则使用当前工作目录。
        """
        self._get_project_root = get_project_root

    def _get_root(self) -> str:
        if self._get_project_root:
            return self._get_project_root()
        return os.getcwd()

    @property
    def name(self) -> str:
        return "memory_save"

    @property
    def description(self) -> str:
        return (
            "Save an important finding or knowledge to the project's cross-session memory. "
            "Stored in .agent/knowledge.jsonl. Automatically deduplicates similar content. "
            "Use this when you discover something useful about the project (e.g., build commands, "
            "code conventions, gotchas, architecture decisions) that future sessions should know."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The knowledge or finding to save"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization (e.g., ['python', 'build', 'gotcha'])"
                },
                "source": {
                    "type": "string",
                    "description": "Source identifier (e.g., session ID or context)"
                },
            },
            "required": ["content"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    async def execute(self, **kwargs: Any) -> str:
        content = kwargs.get("content")
        tags = kwargs.get("tags", [])
        source = kwargs.get("source", "")

        if not content:
            raise ToolExecutionError(self.name, "content is required")

        try:
            root = self._get_root()
            manager = ProjectMemoryManager(root)

            # 自动初始化（如果尚未初始化）
            if not manager.is_initialized:
                manager.init_project()

            entry = manager.save_knowledge(
                content=content,
                tags=tags if isinstance(tags, list) else [],
                source=source,
            )

            return (
                f"Saved to project memory: \"{content[:80]}{'...' if len(content) > 80 else ''}\"\n"
                f"Tags: {tags or '(none)'}\n"
                f"ID: {entry.id}"
            )
        except Exception as e:
            raise ToolExecutionError(self.name, str(e))


class MemorySearchTool(Tool):
    """
    搜索项目记忆

    支持关键词搜索和标签过滤，从 .agent/knowledge.jsonl 中检索历史知识。
    """

    def __init__(self, get_project_root: Any = None) -> None:
        self._get_project_root = get_project_root

    def _get_root(self) -> str:
        if self._get_project_root:
            return self._get_project_root()
        return os.getcwd()

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search the project's cross-session memory for previously saved knowledge. "
            "Supports keyword search (matches content) and tag filtering. "
            "Use this to recall facts learned in earlier sessions about the project."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords (matches content and tags)"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by tags (all must match)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 10)"
                },
            },
            "required": [],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query", "")
        tags = kwargs.get("tags", [])
        limit = kwargs.get("limit", 10)

        try:
            root = self._get_root()
            manager = ProjectMemoryManager(root)

            if not manager.is_initialized:
                return "No project memory found. The .agent/ directory has not been initialized."

            results = manager.search_knowledge(
                query=query,
                tags=tags if isinstance(tags, list) else [],
                limit=limit,
            )

            if not results:
                return f"No knowledge found matching query='{query}', tags={tags or 'any'}"

            lines = [f"Found {len(results)} knowledge entries:\n"]
            for entry in results:
                tag_str = f" [{', '.join(entry.tags)}]" if entry.tags else ""
                source_str = f" (from: {entry.source})" if entry.source else ""
                lines.append(f"- {entry.content}{tag_str}{source_str}")

            return "\n".join(lines)
        except Exception as e:
            raise ToolExecutionError(self.name, str(e))


class MemoryReadTool(Tool):
    """
    读取项目上下文

    读取 PROJECT.md 内容，供 Agent 启动时注入上下文。
    """

    def __init__(self, get_project_root: Any = None) -> None:
        self._get_project_root = get_project_root

    def _get_root(self) -> str:
        if self._get_project_root:
            return self._get_project_root()
        return os.getcwd()

    @property
    def name(self) -> str:
        return "memory_read"

    @property
    def description(self) -> str:
        return (
            "Read the project's PROJECT.md and recent knowledge summary. "
            "This is typically called automatically at agent startup to inject project context."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        try:
            root = self._get_root()
            manager = ProjectMemoryManager(root)

            if not manager.is_initialized:
                return "No project memory found. Use memory_save to start building project knowledge."

            context = manager.get_context_for_agent()
            return context if context else "Project memory is empty."
        except Exception as e:
            raise ToolExecutionError(self.name, str(e))


def register_memory_tools(get_project_root: Any = None) -> None:
    """
    注册记忆工具到工具注册中心

    Args:
        get_project_root: 可调用对象，返回当前项目根目录。
                          如果为 None，则使用当前工作目录。
    """
    from .registry import register_tool

    register_tool(MemorySaveTool(get_project_root=get_project_root))
    register_tool(MemorySearchTool(get_project_root=get_project_root))
    register_tool(MemoryReadTool(get_project_root=get_project_root))
