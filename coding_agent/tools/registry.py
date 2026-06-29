"""
工具注册中心 - 管理所有可用工具

参考 Claude Code 的工具池组装：
1. Base enumeration
2. Mode filtering
3. Deny pre-filtering
4. MCP integration
5. Deduplication
"""
from __future__ import annotations

from typing import Any
from .base import Tool, ToolPermission, Hook, HookEvent, HookContext


class ToolRegistry:
    """
    工具注册中心
    
    负责：
    - 注册/注销工具
    - 根据权限过滤工具
    - 执行 hook
    - 获取工具定义列表
    """
    
    def __init__(self, max_result_chars: int = 30000) -> None:
        self._tools: dict[str, Tool] = {}
        self._hooks: dict[HookEvent, list[Hook]] = {event: [] for event in HookEvent}
        self._disabled_tools: set[str] = set()
        # 单条工具结果的字符上限：超过则 head+tail 截断，
        # 防止单次巨大输出（冗长构建/大范围 grep）撑爆下一次模型请求。
        self.max_result_chars = max_result_chars
    
    def register(self, tool: Tool) -> None:
        """注册工具"""
        self._tools[tool.name] = tool
    
    def unregister(self, tool_name: str) -> None:
        """注销工具"""
        self._tools.pop(tool_name, None)
    
    def disable(self, tool_name: str) -> None:
        """禁用工具"""
        self._disabled_tools.add(tool_name)
    
    def enable(self, tool_name: str) -> None:
        """启用工具"""
        self._disabled_tools.discard(tool_name)
    
    def get_tool(self, name: str) -> Tool | None:
        """获取工具"""
        if name in self._disabled_tools:
            return None
        return self._tools.get(name)
    
    def get_all_tools(self) -> list[Tool]:
        """获取所有可用工具"""
        return [
            tool for name, tool in self._tools.items()
            if name not in self._disabled_tools
        ]
    
    def get_tools_by_permission(self, max_permission: ToolPermission) -> list[Tool]:
        """根据权限级别过滤工具"""
        permission_order = {
            ToolPermission.READ: 0,
            ToolPermission.WRITE: 1,
            ToolPermission.EXECUTE: 2,
            ToolPermission.DANGEROUS: 3,
        }
        max_level = permission_order[max_permission]
        return [
            tool for tool in self.get_all_tools()
            if permission_order[tool.permission] <= max_level
        ]
    
    def get_openai_functions(self, max_permission: ToolPermission | None = None) -> list[dict[str, Any]]:
        """获取 OpenAI function calling 格式的工具列表"""
        if max_permission:
            tools = self.get_tools_by_permission(max_permission)
        else:
            tools = self.get_all_tools()
        return [
            {
                "type": "function",
                "function": tool.get_openai_function()
            }
            for tool in tools
        ]
    
    # Hook 系统
    def add_hook(self, event: HookEvent, hook: Hook) -> None:
        """添加 hook"""
        self._hooks[event].append(hook)
    
    def remove_hook(self, event: HookEvent, hook: Hook) -> None:
        """移除 hook"""
        if hook in self._hooks[event]:
            self._hooks[event].remove(hook)
    
    async def run_hooks(self, event: HookEvent, context: HookContext) -> bool:
        """
        执行 hook
        
        Returns:
            True 如果任何 hook 阻止了操作
        """
        for hook in self._hooks[event]:
            result = hook(context)
            if result is True:
                return True
        return False

    def _truncate_result(self, result: str) -> str:
        """
        把超长工具结果截断为 head + tail，并标注省略的字符数。

        保留头尾通常比单纯保留头部更有用：错误/摘要往往在末尾，
        而命令/文件开头也常包含关键信息。
        """
        if not isinstance(result, str):
            return result
        cap = self.max_result_chars
        if cap <= 0 or len(result) <= cap:
            return result
        head = int(cap * 0.7)
        tail = cap - head
        omitted = len(result) - head - tail
        return (
            result[:head]
            + f"\n\n... [{omitted} characters truncated by tool-output limit] ...\n\n"
            + result[-tail:]
        )

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """
        执行工具
        
        完整流程：
        1. 查找工具
        2. 运行 pre_tool_use hook
        3. 执行工具
        4. 运行 post_tool_use hook
        5. 返回结果
        """
        tool = self.get_tool(name)
        if not tool:
            return f"Error: Tool '{name}' not found or disabled"
        
        # Pre-tool hook
        pre_context = HookContext(
            event=HookEvent.PRE_TOOL_USE,
            tool_name=name,
            tool_args=arguments
        )
        if await self.run_hooks(HookEvent.PRE_TOOL_USE, pre_context):
            return f"Error: Tool '{name}' execution blocked by hook"
        
        try:
            result = await tool.execute(**arguments)

            # 边界保护：截断超长结果（head+tail）
            result = self._truncate_result(result)

            # Post-tool hook
            post_context = HookContext(
                event=HookEvent.POST_TOOL_USE,
                tool_name=name,
                tool_args=arguments,
                tool_result=result
            )
            await self.run_hooks(HookEvent.POST_TOOL_USE, post_context)

            return result
        except Exception as e:
            # Error hook
            error_context = HookContext(
                event=HookEvent.ON_ERROR,
                tool_name=name,
                tool_args=arguments,
                error=e
            )
            await self.run_hooks(HookEvent.ON_ERROR, error_context)
            return f"Error executing tool '{name}': {str(e)}"


# 全局工具注册中心实例
_global_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """获取全局工具注册中心"""
    global _global_registry
    if _global_registry is None:
        _global_registry = ToolRegistry()
    return _global_registry


def register_tool(tool: Tool) -> None:
    """注册工具到全局注册中心"""
    get_registry().register(tool)
