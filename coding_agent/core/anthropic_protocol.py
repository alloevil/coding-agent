"""
Anthropic Messages 协议适配 - 在 OpenAI 形态与 Anthropic 形态之间翻译

ModelClient 内部统一用 OpenAI 形态（messages 带 role/content/tool_calls，
tools 是 {"type":"function","function":{...}}，结果是 {content, tool_calls, ...}）。
本模块把这套形态翻译到 Anthropic /v1/messages 协议、并把响应翻译回来，
这样 agent loop 无需感知后端差异。

Anthropic 关键差异：
  - system 是顶层独立字段，不在 messages 里
  - content 是 block 数组：{type:"text"} / {type:"tool_use"} / {type:"tool_result"}
  - 工具调用是 assistant 的 tool_use block；工具结果是 user 的 tool_result block
  - 工具定义是 {name, description, input_schema}
  - 流式是 SSE 事件：content_block_delta(text_delta / input_json_delta) 等

只做纯函数翻译 + SSE 累积，便于离线单测（无需真端点）。
"""
from __future__ import annotations

import json
from typing import Any


def to_anthropic_request(messages: list[dict[str, Any]],
                         tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    """把 OpenAI 形态的 messages/tools 翻译成 Anthropic 请求片段。

    返回 {"system": str|None, "messages": [...], "tools": [...]|None}。
    """
    system_parts: list[str] = []
    out_messages: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue
        if role == "tool":
            # OpenAI 的 tool 结果 → Anthropic user 的 tool_result block
            out_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content if isinstance(content, str) else json.dumps(content),
                }],
            })
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": args,
                })
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            out_messages.append({"role": "assistant", "content": blocks})
            continue
        # user（或其它）：直接作为文本
        if isinstance(content, str):
            out_messages.append({"role": "user", "content": content})
        elif isinstance(content, list):
            out_messages.append({"role": "user", "content": content})

    result: dict[str, Any] = {
        "system": "\n\n".join(system_parts) if system_parts else None,
        "messages": out_messages,
    }
    if tools:
        result["tools"] = [_tool_to_anthropic(t) for t in tools]
    return result


def _tool_to_anthropic(tool: dict[str, Any]) -> dict[str, Any]:
    """OpenAI function 工具 → Anthropic 工具 {name, description, input_schema}。"""
    fn = tool.get("function") if "function" in tool else tool
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }


def from_anthropic_response(data: dict[str, Any]) -> dict[str, Any]:
    """把 Anthropic 非流式响应翻译回 OpenAI 形态结果 dict。"""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in data.get("content", []):
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })
    return {
        "content": "".join(text_parts),
        "tool_calls": tool_calls,
        "reasoning": "",
        "usage": _usage_to_openai(data.get("usage")),
    }


def _usage_to_openai(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Anthropic usage → OpenAI usage 形态（供 ModelClient._record_usage 复用）。"""
    if not usage:
        return {}
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    return {
        "prompt_tokens": (usage.get("input_tokens", 0) or 0) + cache_read,
        "completion_tokens": usage.get("output_tokens", 0) or 0,
        "prompt_tokens_details": {"cached_tokens": cache_read},
    }


class AnthropicStreamAccumulator:
    """累积 Anthropic SSE 事件，产出与非流式一致的 OpenAI 形态结果。

    处理的事件：
      - content_block_start: 记录新 block（text / tool_use）
      - content_block_delta: text_delta（正文）/ input_json_delta（工具参数片段）
      - message_delta / message_start: 抓 usage
    """

    def __init__(self, on_text_delta=None, on_reasoning_delta=None):
        self._on_text = on_text_delta
        self._on_reasoning = on_reasoning_delta
        self._blocks: dict[int, dict[str, Any]] = {}
        self._text_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self.usage: dict[str, Any] = {}

    def feed(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "content_block_start":
            idx = event.get("index", 0)
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                self._blocks[idx] = {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "args": "",
                }
        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            dtype = delta.get("type")
            if dtype == "text_delta":
                chunk = delta.get("text", "")
                if chunk:
                    self._text_parts.append(chunk)
                    if self._on_text is not None:
                        self._on_text(chunk)
            elif dtype == "thinking_delta":
                # extended-thinking：推理逐字流式（与正文分开）
                chunk = delta.get("thinking", "")
                if chunk:
                    self._reasoning_parts.append(chunk)
                    if self._on_reasoning is not None:
                        self._on_reasoning(chunk)
            elif dtype == "input_json_delta":
                idx = event.get("index", 0)
                if idx in self._blocks:
                    self._blocks[idx]["args"] += delta.get("partial_json", "")
        elif etype in ("message_start", "message_delta"):
            msg = event.get("message", {})
            u = msg.get("usage") or event.get("usage")
            if u:
                # message_delta 通常只带 output_tokens 增量；合并
                self.usage = {**self.usage, **u}

    def result(self) -> dict[str, Any]:
        tool_calls = []
        for idx in sorted(self._blocks):
            b = self._blocks[idx]
            tool_calls.append({
                "id": b["id"],
                "type": "function",
                "function": {"name": b["name"], "arguments": b["args"] or "{}"},
            })
        return {
            "content": "".join(self._text_parts),
            "tool_calls": tool_calls,
            "reasoning": "".join(self._reasoning_parts),
            "usage": _usage_to_openai(self.usage),
        }
