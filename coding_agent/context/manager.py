"""
Context 管理 - 组装 + 压缩

参考 Claude Code 的 5 层压缩设计：
1. Budget Reduction - 预算削减
2. Snip - 裁剪
3. Microcompact - 微压缩
4. Context Collapse - 上下文折叠
5. Auto-Compact - 自动压缩
"""
from __future__ import annotations

from typing import Any
from ..core.state import AgentState, Message, MessageRole


class ContextManager:
    """
    Context 管理器
    
    负责：
    - 组装发送给模型的 context
    - 在 context 过长时进行压缩
    """
    
    def __init__(self, max_tokens: int = 200000, load_project_context: bool = True):
        self.max_tokens = max_tokens
        self.compression_threshold = 0.9  # 90% 时触发压缩
        self._load_project_context = load_project_context
        # 项目上下文只加载一次并缓存（会话生命周期内通常不变）
        self._project_context_cache: str | None = None

    def _get_project_context(self) -> str:
        """加载（并缓存）AGENTS.md/CLAUDE.md 项目上下文。"""
        if not self._load_project_context:
            return ""
        if self._project_context_cache is None:
            from .project_context import load_project_context
            try:
                self._project_context_cache = load_project_context()
            except Exception:
                self._project_context_cache = ""
        return self._project_context_cache

    def assemble_context(self, state: AgentState, system_prompt: str) -> list[dict[str, Any]]:
        """
        组装 context

        有序来源：
        1. System prompt
        2. 项目上下文（AGENTS.md / CLAUDE.md 层级合并）
        3. 会话历史
        """
        messages = []

        # 1. System prompt
        if system_prompt:
            messages.append({
                "role": "system",
                "content": system_prompt
            })

        # 2. 项目上下文（AGENTS.md / CLAUDE.md）
        project_ctx = self._get_project_context()
        if project_ctx:
            messages.append({
                "role": "system",
                "content": project_ctx
            })

        # 3. 添加会话历史
        for msg in state.messages:
            messages.append(msg.to_dict())

        return messages
    
    def needs_compaction(self, state: AgentState) -> bool:
        """检查是否需要压缩"""
        estimated_tokens = state.get_token_estimate()
        return estimated_tokens > self.max_tokens * self.compression_threshold
    
    async def compact(self, state: AgentState, model_call_fn: Any) -> None:
        """
        执行 context 压缩
        
        参考 Claude Code 的 5 层压缩策略，从便宜到贵：
        1. Budget Reduction - 简单截断
        2. Snip - 裁剪不重要的部分
        3. Microcompact - 微压缩
        4. Context Collapse - 上下文折叠
        5. Auto-Compact - 自动压缩（需要模型调用）
        """
        # 尝试便宜的压缩方式
        if self._try_budget_reduction(state):
            return
        
        if self._try_snip(state):
            return
        
        # 如果便宜的方式不够，使用模型压缩
        await self._auto_compact(state, model_call_fn)
    
    def _try_budget_reduction(self, state: AgentState) -> bool:
        """
        第 1 层：Budget Reduction - 简单截断
        
        策略：移除最旧的消息，保留最近的消息
        """
        estimated_tokens = state.get_token_estimate()
        target_tokens = int(self.max_tokens * 0.7)  # 目标降到 70%
        
        if estimated_tokens <= target_tokens:
            return True
        
        # 移除最旧的消息（保留系统消息和最近的消息）
        while estimated_tokens > target_tokens and len(state.messages) > 10:
            # 移除第 0 条消息
            removed = state.messages.pop(0)
            estimated_tokens -= self._estimate_message_tokens(removed)
        
        return estimated_tokens <= target_tokens
    
    def _try_snip(self, state: AgentState) -> bool:
        """
        第 2 层：Snip - 裁剪
        
        策略：截断过长的工具结果
        """
        max_tool_result_length = 5000  # 最大工具结果长度
        
        for msg in state.messages:
            if msg.role == MessageRole.TOOL and msg.tool_result:
                if len(msg.tool_result.content) > max_tool_result_length:
                    # 截断并添加省略号
                    msg.tool_result.content = (
                        msg.tool_result.content[:max_tool_result_length] 
                        + "\n... (truncated)"
                    )
        
        return state.get_token_estimate() <= self.max_tokens * 0.8
    
    async def _auto_compact(self, state: AgentState, model_call_fn: Any) -> None:
        """
        第 5 层：Auto-Compact - 自动压缩
        
        策略：使用模型总结历史对话
        """
        if not model_call_fn:
            return
        
        # 构建总结请求
        summary_prompt = """Please provide a concise summary of the conversation so far, focusing on:
1. What we've accomplished
2. What we're currently working on
3. Key decisions made
4. Any pending tasks

Keep the summary under 1000 words."""
        
        # 调用模型生成总结
        try:
            summary = await model_call_fn([
                {"role": "user", "content": summary_prompt}
            ])
            
            # 用总结替换历史消息
            state.messages = [
                Message(
                    role=MessageRole.ASSISTANT,
                    content=f"[Context Summary]\n{summary}"
                )
            ]
        except Exception:
            # 如果总结失败，使用简单的截断
            self._try_budget_reduction(state)
    
    def _estimate_message_tokens(self, message: Message) -> int:
        """估算单条消息的 token 数"""
        if message.content:
            if isinstance(message.content, str):
                return len(message.content) // 4
            elif isinstance(message.content, list):
                total = 0
                for item in message.content:
                    if isinstance(item, dict) and "text" in item:
                        total += len(item["text"]) // 4
                return total
        return 0
