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
    
    def __init__(self, max_result_chars: int = 30000,
                 default_tool_timeout: float | None = 120.0) -> None:
        self._tools: dict[str, Tool] = {}
        self._hooks: dict[HookEvent, list[Hook]] = {event: [] for event in HookEvent}
        self._disabled_tools: set[str] = set()
        # 单条工具结果的字符上限：超过则 head+tail 截断，
        # 防止单次巨大输出（冗长构建/大范围 grep）撑爆下一次模型请求。
        self.max_result_chars = max_result_chars
        # 单个工具执行的默认超时（秒）。防止挂死的 MCP/网络/LSP 调用
        # 永久冻结整个 agent。None/<=0 表示不限制。
        # 自带超时的工具（如 shell_exec）通过 _self_timed_tools 豁免。
        self.default_tool_timeout = default_tool_timeout
        self._self_timed_tools: set[str] = {"shell_exec"}
        # 截断溢出目录：超长结果的全文写到这里，预览里附路径供按需读取。
        # None → 用系统临时目录下的 coding-agent-tool-output/。
        self._spill_dir: str | None = None
        self._spill_counter = 0
    
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

    def _spill_full_output(self, result: str) -> str | None:
        """把超长结果全文写到溢出目录，返回文件路径（失败返回 None）。"""
        import os
        import tempfile
        try:
            base = self._spill_dir or os.path.join(
                tempfile.gettempdir(), "coding-agent-tool-output")
            os.makedirs(base, exist_ok=True)
            self._spill_counter += 1
            path = os.path.join(base, f"output-{os.getpid()}-{self._spill_counter}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(result)
            return path
        except OSError:
            return None

    def _truncate_result(self, result: str) -> str:
        """
        把超长工具结果截断为 head + tail，并标注省略的字符数。

        保留头尾通常比单纯保留头部更有用：错误/摘要往往在末尾，
        而命令/文件开头也常包含关键信息。此外把全文写到溢出文件，
        在预览里给出路径，模型可按需用 file_read 读取完整内容
        （参考 opencode 的 truncation-dir）。
        """
        if not isinstance(result, str):
            return result
        cap = self.max_result_chars
        if cap <= 0 or len(result) <= cap:
            return result
        head = int(cap * 0.7)
        tail = cap - head
        omitted = len(result) - head - tail
        spill = self._spill_full_output(result)
        hint = (f" full output saved to {spill} — read it for the rest"
                if spill else "")
        return (
            result[:head]
            + f"\n\n... [{omitted} characters truncated by tool-output limit;{hint}] ...\n\n"
            + result[-tail:]
        )

    def _effective_timeout(self, name: str, tool: Tool) -> float | None:
        """解析某工具的有效超时：工具自带 timeout_seconds 优先；自计时工具豁免。"""
        if name in self._self_timed_tools:
            return None
        override = getattr(tool, "timeout_seconds", None)
        if override is not None:
            return override if override and override > 0 else None
        t = self.default_tool_timeout
        return t if t and t > 0 else None

    async def _execute_with_timeout(self, name: str, tool: Tool,
                                    arguments: dict[str, Any]) -> str:
        """执行工具，必要时套 asyncio.wait_for，超时返回明确错误而非冻结。"""
        import asyncio

        timeout = self._effective_timeout(name, tool)
        if timeout is None:
            return await tool.execute(**arguments)
        try:
            return await asyncio.wait_for(tool.execute(**arguments), timeout=timeout)
        except asyncio.TimeoutError:
            return (f"Error: tool '{name}' timed out after {timeout:.0f}s. "
                    f"It may be stuck (slow network, hung process, or large input). "
                    f"Try a narrower request or a smaller scope.")

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
            result = await self._execute_with_timeout(name, tool, arguments)

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
