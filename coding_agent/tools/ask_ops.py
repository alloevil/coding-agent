"""
ask_user 工具 - 让 agent 向用户提一个结构化问题

参考 Claude Code 的 AskUserQuestion 与 opencode 的 question 工具：当 agent
遇到无法从上下文/代码确定、且需要用户拍板的选择时，提一个带选项的问题，
而不是猜。回答通过外部注入的 question handler 取得（CLI 走 input()，
protocol 走 JSON 往返）。

handler 签名：async (question: str, options: list[str]) -> str
  返回用户选择的文本（可能是某个 option，也可能是自由输入）。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from .base import Tool, ToolPermission

# 问题处理器：返回用户的回答字符串
QuestionHandler = Callable[[str, list[str]], Awaitable[str]]


class AskUserTool(Tool):
    """向用户提一个结构化问题并取得回答。"""

    def __init__(self, handler: QuestionHandler | None = None) -> None:
        self._handler = handler

    def set_handler(self, handler: QuestionHandler) -> None:
        self._handler = handler

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return (
            "Ask the user a question when you genuinely cannot decide from the "
            "request, the code, or sensible defaults — e.g. ambiguous requirements "
            "or a choice between real alternatives. Provide options when you can. "
            "Returns the user's answer. Do not use for things you can determine "
            "yourself."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask the user.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of suggested answers.",
                },
            },
            "required": ["question"],
        }

    @property
    def permission(self) -> ToolPermission:
        # 交互式、无副作用；自动放行（实际由 handler 阻塞等待用户）
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        question = kwargs.get("question")
        options = kwargs.get("options") or []
        if not question:
            return "Error: 'question' is required"
        if self._handler is None:
            # 无 handler（如无人值守环境）：明确返回，让模型用默认值继续
            return ("No interactive user available to answer. Proceed with a "
                    "reasonable default and state the assumption you made.")
        try:
            answer = await self._handler(question, list(options))
        except Exception as e:
            return f"Error obtaining user answer: {e}"
        return f"User answered: {answer}"


def register_ask_tools(handler: QuestionHandler | None = None, registry: Any = None) -> AskUserTool:
    """注册 ask_user 工具，返回实例以便后续注入 handler。"""
    from .registry import get_registry

    reg = registry or get_registry()
    tool = AskUserTool(handler=handler)
    reg.register(tool)
    return tool
