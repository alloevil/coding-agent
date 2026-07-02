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

import json
from typing import Any
from ..core.state import AgentState, Message, MessageRole


class ContextManager:
    """
    Context 管理器
    
    负责：
    - 组装发送给模型的 context
    - 在 context 过长时进行压缩
    """
    
    def __init__(self, max_tokens: int = 200000, load_project_context: bool = True,
                 extra_system_provider: Any = None):
        self.max_tokens = max_tokens
        self.compression_threshold = 0.9  # 90% 时触发压缩
        self._load_project_context = load_project_context
        # 项目上下文只加载一次并缓存（会话生命周期内通常不变）
        self._project_context_cache: str | None = None
        self._project_ctx_touched_count: int = -1
        # 可选：额外 system 块提供者（() -> str），如可用 skills 清单。
        # 解耦：context manager 不直接依赖 skills 模块。
        self._extra_system_provider = extra_system_provider

    def _get_project_context(self) -> str:
        """加载（并缓存）AGENTS.md/CLAUDE.md 项目上下文。

        缓存键含"已读目录"数量：agent 进入新子目录读文件后，缓存失效以
        纳入该目录的嵌套上下文文件。
        """
        if not self._load_project_context:
            return ""
        from .project_context import load_project_context, _TOUCHED_DIRS
        touched = len(_TOUCHED_DIRS)
        if self._project_context_cache is None or touched != self._project_ctx_touched_count:
            try:
                self._project_context_cache = load_project_context()
            except Exception:
                self._project_context_cache = ""
            self._project_ctx_touched_count = touched
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

        # 2b. 额外 system 块（如可用 skills 清单）——渐进式披露：只注入
        #     name+description，完整指令由模型按需通过 skill 工具加载。
        if self._extra_system_provider is not None:
            try:
                extra = self._extra_system_provider()
            except Exception:
                extra = ""
            if extra:
                messages.append({"role": "system", "content": extra})

        # 3. 添加会话历史
        for msg in state.messages:
            messages.append(msg.to_dict())

        # 4. 回灌当前计划（若有）——让模型每轮都能看见自己的待办，
        #    放在历史之后作为最新提醒（参考 Claude Code 的 todo 常驻上下文）。
        plan_block = self._render_plan(state)
        if plan_block:
            messages.append({"role": "system", "content": plan_block})

        # 5. 一次性交接提醒（plan→build 切换）：注入后即消费，避免每轮重复。
        if state.metadata:
            note = state.metadata.pop("pending_handoff", None)
            if note:
                messages.append({"role": "system", "content": note})

        return messages

    def _render_plan(self, state: AgentState) -> str:
        """把 state.metadata 里的当前计划渲染成一段提醒文本（无则返回空串）。"""
        plan = state.metadata.get("plan") if state.metadata else None
        if not plan:
            return ""
        try:
            from ..tools.plan_ops import render_plan
            return "Current plan (keep this updated via update_plan):\n" + render_plan(plan)
        except Exception:
            return ""

    def needs_compaction(self, state: AgentState) -> bool:
        """检查是否需要压缩"""
        estimated_tokens = state.get_token_estimate()
        return estimated_tokens > self.max_tokens * self.compression_threshold

    # microcompact 触发阈值：远早于全量压缩（默认 90%），在 60% 就开始
    # 悄悄回收旧工具输出，把昂贵的模型总结推迟乃至避免。
    microcompact_threshold = 0.6
    # 最近这么多条消息内的工具结果一律不动（可能还要被引用）。
    MICROCOMPACT_KEEP_RECENT = 8
    # 短于此的工具结果不值得回收（占位符本身也占字符）。
    MICROCOMPACT_MIN_LEN = 400
    _ELIDED = "[tool output elided to save context]"

    def needs_microcompaction(self, state: AgentState) -> bool:
        """是否该做 microcompact：超过较低阈值即可（比 needs_compaction 早）。"""
        return state.get_token_estimate() > self.max_tokens * self.microcompact_threshold

    def microcompact(self, state: AgentState) -> int:
        """
        Microcompact - 微压缩（学 Claude Code 的做法，纯本地、不调模型）。

        只回收「较旧且已完成」的工具结果：把它们的 content 替换成占位符，
        保留 assistant 的推理/决策文本和 tool_call 结构（配对不破），也保留
        最近 MICROCOMPACT_KEEP_RECENT 条。这是外科手术式回收，区别于全量总结
        那种「一刀切丢掉所有旧历史」。返回回收的消息数。

        幂等：已是占位符的不再动。
        """
        msgs = state.messages
        cutoff = len(msgs) - self.MICROCOMPACT_KEEP_RECENT
        elided = 0
        for i, msg in enumerate(msgs):
            if i >= cutoff:
                break  # 最近窗口内不动
            if msg.role != MessageRole.TOOL or not msg.tool_result:
                continue
            content = msg.tool_result.content
            if content == self._ELIDED or len(content) < self.MICROCOMPACT_MIN_LEN:
                continue
            msg.tool_result.content = self._ELIDED
            elided += 1
        return elided

    async def compact(self, state: AgentState, model_call_fn: Any) -> None:
        """
        执行 context 压缩，从便宜到贵：
        0. Microcompact - 回收旧的已完成工具输出（本地、最便宜）
        1. Snip - 截断过长的工具结果（不丢消息）
        2. Auto-Compact - 用模型总结较旧的历史，保留最近若干轮原文
        3. Budget Reduction - 兜底硬截断（保持 tool 配对完整）
        """
        # 第 0 层：microcompact（本地回收旧工具输出，往往就够了）
        self.microcompact(state)
        if state.get_token_estimate() <= self.max_tokens * 0.8:
            return

        # 第 1 层：先尝试截断超长工具结果（最便宜，不丢消息）
        self._try_snip(state)
        if state.get_token_estimate() <= self.max_tokens * 0.8:
            return

        # 第 2 层：模型总结较旧历史
        if model_call_fn is not None:
            try:
                await self._auto_compact(state, model_call_fn)
                if state.get_token_estimate() <= self.max_tokens * 0.8:
                    return
            except Exception:
                pass  # 落到兜底截断

        # 第 3 层：兜底硬截断
        self._try_budget_reduction(state)

    # 最近保留的轮次数（用户/助手回合），不参与总结
    RECENT_KEEP_MESSAGES = 12

    def _safe_split_index(self, messages: list[Message], desired: int) -> int:
        """
        给定一个期望的保留起点 desired（保留 messages[desired:]），
        向后调整到一个“安全”边界：保留窗口的第一条不能是 tool 结果消息
        （否则它会孤立于其 assistant tool_calls 之外，导致 API 报错）。

        返回调整后的 split index（保证保留窗口自洽）。
        """
        idx = max(0, min(desired, len(messages)))
        # 若保留窗口以 tool 结果开头，向后推进直到不是 tool 消息
        while idx < len(messages) and messages[idx].role == MessageRole.TOOL:
            idx += 1
        return idx

    def _try_budget_reduction(self, state: AgentState) -> bool:
        """
        Budget Reduction - 硬截断。

        移除最旧的消息，保留最近的消息；保证截断后保留窗口不以
        孤立的 tool 结果开头。
        """
        target_tokens = int(self.max_tokens * 0.7)

        if state.get_token_estimate() <= target_tokens:
            return True

        msgs = state.messages
        # 从前往后累计要丢弃的消息，直到剩余 token 达标且至少保留几条
        drop = 0
        running = state.get_token_estimate()
        while drop < len(msgs) - 4 and running > target_tokens:
            running -= self._estimate_message_tokens(msgs[drop])
            drop += 1

        split = self._safe_split_index(msgs, drop)
        state.messages = msgs[split:]
        return state.get_token_estimate() <= target_tokens

    def _try_snip(self, state: AgentState) -> bool:
        """
        Snip - 裁剪：截断过长的工具结果。
        
        策略：
        - 最近 6 条消息的工具结果：宽松截断（保留较多内容）
        - 更早的消息：激进截断（只保留头部摘要）
        """
        recent_threshold = 6
        recent_max = 8000   # 最近消息：保留 8K chars
        old_max = 2000      # 旧消息：只保留 2K chars
        head_ratio = 0.7    # 头部占比

        for i, msg in enumerate(state.messages):
            if msg.role != MessageRole.TOOL or not msg.tool_result:
                continue
            content = msg.tool_result.content
            is_recent = (len(state.messages) - i) <= recent_threshold
            max_len = recent_max if is_recent else old_max

            if len(content) > max_len:
                head = int(max_len * head_ratio)
                tail = max_len - head
                omitted = len(content) - head - tail
                msg.tool_result.content = (
                    content[:head]
                    + f"\n... ({omitted} chars truncated) ...\n"
                    + content[-tail:]
                )

        return state.get_token_estimate() <= self.max_tokens * 0.8

    async def _auto_compact(self, state: AgentState, model_call_fn: Any) -> None:
        """
        Auto-Compact - 用模型总结较旧的历史。

        策略（参考 Claude Code）：保留最近 RECENT_KEEP_MESSAGES 条原文，
        把更早的历史交给模型总结为一段文字，作为一条 system 消息前置。
        这样既回收了 token，又不破坏最近的 tool_call/tool_result 配对。
        """
        if not model_call_fn:
            return

        msgs = state.messages
        # 计算保留窗口起点（安全边界）
        desired = max(0, len(msgs) - self.RECENT_KEEP_MESSAGES)
        split = self._safe_split_index(msgs, desired)

        older = msgs[:split]
        recent = msgs[split:]

        if not older:
            # 没有可总结的旧历史，退回硬截断
            self._try_budget_reduction(state)
            return

        # 把旧历史渲染成可读文本喂给模型
        transcript = self._render_transcript(older)
        summary_request = [
            {
                "role": "system",
                "content": (
                    "You are summarizing an earlier portion of a coding-agent "
                    "conversation so it can be dropped from context. Produce a "
                    "concise but information-dense summary covering: user goals, "
                    "decisions made, files created/modified (with paths), key "
                    "command outputs, and any still-pending tasks. Under 800 words."
                ),
            },
            {"role": "user", "content": transcript},
        ]

        # 真实签名是 (context, tools)；总结调用不需要工具
        response = await model_call_fn(summary_request, [])
        summary_text = response.get("content", "") if isinstance(response, dict) else str(response)
        if not summary_text.strip():
            # 总结为空，退回硬截断
            self._try_budget_reduction(state)
            return

        summary_msg = Message(
            role=MessageRole.SYSTEM,
            content=f"[Earlier conversation summary]\n{summary_text}",
        )
        state.messages = [summary_msg] + recent

    def _render_transcript(self, messages: list[Message]) -> str:
        """把消息列表渲染成可读文本，供总结使用。"""
        lines: list[str] = []
        for msg in messages:
            role = msg.role.value
            if msg.tool_result:
                lines.append(f"[tool result] {msg.tool_result.content}")
            elif msg.tool_calls:
                calls = ", ".join(
                    f"{tc.name}({json.dumps(tc.arguments, ensure_ascii=False)})"
                    for tc in msg.tool_calls
                )
                text = msg.content or ""
                lines.append(f"[{role}] {text}\n[tool calls] {calls}")
            elif isinstance(msg.content, str):
                lines.append(f"[{role}] {msg.content}")
            elif isinstance(msg.content, list):
                joined = " ".join(
                    item.get("text", "") for item in msg.content
                    if isinstance(item, dict)
                )
                lines.append(f"[{role}] {joined}")
        return "\n".join(lines)

    def _estimate_message_tokens(self, message: Message) -> int:
        """估算单条消息的 token 数（含工具调用参数与工具结果）。"""
        total = 0
        if message.content:
            if isinstance(message.content, str):
                total += len(message.content) // 4
            elif isinstance(message.content, list):
                for item in message.content:
                    if isinstance(item, dict) and "text" in item:
                        total += len(item["text"]) // 4
        if message.tool_calls:
            for tc in message.tool_calls:
                total += (len(tc.name) + len(json.dumps(tc.arguments))) // 4
        if message.tool_result and message.tool_result.content:
            total += len(message.tool_result.content) // 4
        return total
