"""
会话标题生成 - 让 --list-sessions / /sessions 列表可辨识

两种方式：
  - heuristic_title(state)：纯本地、零成本。取第一条用户消息，清洗成一行短标题。
    始终可用，作为兜底。
  - generate_title(state, model_call_fn)：可选，让模型生成更贴切的一行标题。
    失败时回退到启发式。

参考 Claude Code / opencode：会话列表用简短标题而非裸 UUID + 时间戳。
"""
from __future__ import annotations

import re
from typing import Any

from .state import AgentState, MessageRole


MAX_TITLE_LEN = 60


def _first_user_text(state: AgentState) -> str:
    """取第一条用户消息的纯文本（content 可能是 str 或 multimodal list）。"""
    for msg in state.messages:
        if msg.role != MessageRole.USER:
            continue
        content = msg.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            joined = " ".join(p for p in parts if p)
            if joined:
                return joined
    return ""


def _clean(text: str) -> str:
    """折叠空白、去掉 slash 命令前缀，截断到一行。"""
    text = text.strip()
    # 去掉前导 slash 命令名（/fix foo → foo），命令本身不该成为标题
    text = re.sub(r"^/\S+\s*", "", text)
    # 折叠所有空白为单空格
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _truncate(text: str, limit: int = MAX_TITLE_LEN) -> str:
    if len(text) <= limit:
        return text
    # 在词边界截断，避免切断单词
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]).rstrip() + "…"


def heuristic_title(state: AgentState) -> str:
    """从第一条用户消息派生一行标题；无内容时返回 'Untitled session'。"""
    raw = _first_user_text(state).strip()
    if not raw:
        return "Untitled session"
    # 先取第一行（在折叠空白之前，否则换行会被抹掉），再清洗
    first_line = _clean(raw.split("\n", 1)[0])
    return _truncate(first_line) if first_line else "Untitled session"


_TITLE_PROMPT = (
    "Summarize this coding session's first request as a terse title of at most 6 "
    "words. Output ONLY the title — no quotes, no punctuation at the end, no "
    "preamble. Request:\n\n"
)


async def generate_title(state: AgentState, model_call_fn: Any) -> str:
    """
    用模型生成一行标题；任何异常/空结果回退到 heuristic_title。

    model_call_fn 与 AgentLoop 注入的同签名：async (messages, tools) -> dict，
    返回里取 'content'。这里不带工具、单轮。
    """
    fallback = heuristic_title(state)
    seed = _clean(_first_user_text(state))
    if not seed or model_call_fn is None:
        return fallback
    try:
        messages = [{"role": "user", "content": _TITLE_PROMPT + seed[:500]}]
        resp = await model_call_fn(messages, [])
        text = (resp or {}).get("content") if isinstance(resp, dict) else None
        if not text:
            return fallback
        title = _clean(str(text)).strip('"').strip("'").rstrip(".")
        return _truncate(title) if title else fallback
    except Exception:
        return fallback
