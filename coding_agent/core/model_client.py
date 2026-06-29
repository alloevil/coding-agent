"""
模型客户端 - 统一的异步 LLM 调用层

参考 Codex / Claude Code 的设计：
- 单一的、可复用的客户端，被 CLI、protocol、SDK 等所有前端共享
- 传输层与表现层解耦：流式文本通过 on_text_delta 回调向外传递，
  调用方自行决定如何呈现（CLI 打印 / protocol 发 JSON 事件）
- API 级别的指数退避重试（区分可重试错误）

OpenAI 兼容（含小米 mify、本地 vLLM 等）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import httpx

# 流式文本增量回调：收到一段文本就调用一次。返回值忽略。
TextDeltaHandler = Callable[[str], None]

# 可重试的 HTTP 状态码（限流 / 网关 / 服务暂时不可用）
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class ModelClient:
    """
    异步 OpenAI 兼容聊天补全客户端。

    用法：
        client = ModelClient(api_key=..., base_url=..., model=...)
        result = await client.complete(messages, tools, on_text_delta=print_fn)
        # result = {"content": str, "tool_calls": list}
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        timeout: float = 120.0,
        max_retries: int = 5,
        base_delay: float = 1.0,
        backoff_factor: float = 2.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.backoff_factor = backoff_factor

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        on_text_delta: TextDeltaHandler | None = None,
        stream: bool = True,
    ) -> dict[str, Any]:
        """
        调用模型，返回 {"content": str, "tool_calls": list}。

        带 API 级别的指数退避重试。流式模式下每段文本通过 on_text_delta 回调。
        """
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                if stream:
                    return await self._complete_stream(messages, tools, on_text_delta)
                return await self._complete_nonstream(messages, tools)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status not in _RETRYABLE_STATUS or attempt >= self.max_retries:
                    raise
                last_exc = e
            except (httpx.TransportError, httpx.TimeoutException) as e:
                # 网络层错误（连接重置、超时等）一律可重试
                if attempt >= self.max_retries:
                    raise
                last_exc = e

            delay = self.base_delay * (self.backoff_factor ** attempt)
            await asyncio.sleep(delay)

        # 理论上不可达
        if last_exc:
            raise last_exc
        raise RuntimeError("Model call failed with no exception captured")

    async def _complete_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        on_text_delta: TextDeltaHandler | None,
    ) -> dict[str, Any]:
        payload = self._payload(messages, tools, stream=True)
        content_parts: list[str] = []
        tool_calls_data: list[dict[str, Any]] = []

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = data.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})

                    if delta.get("content"):
                        chunk = delta["content"]
                        content_parts.append(chunk)
                        if on_text_delta is not None:
                            on_text_delta(chunk)

                    if delta.get("tool_calls"):
                        _accumulate_tool_calls(tool_calls_data, delta["tool_calls"])

        return {
            "content": "".join(content_parts),
            "tool_calls": tool_calls_data,
        }

    async def _complete_nonstream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        payload = self._payload(messages, tools, stream=False)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices", [])
        if not choices:
            return {"content": "", "tool_calls": []}
        message = choices[0].get("message", {})
        return {
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls") or [],
        }


def _accumulate_tool_calls(
    acc: list[dict[str, Any]],
    deltas: list[dict[str, Any]],
) -> None:
    """
    累积流式工具调用增量。

    OpenAI 流式协议下，工具调用按 index 分片到达，name 一次性给出，
    arguments 以多个片段拼接。这里按 index 槽位累积。
    """
    for tc_delta in deltas:
        tc_index = tc_delta.get("index", 0)
        while len(acc) <= tc_index:
            acc.append({"id": "", "type": "function",
                        "function": {"name": "", "arguments": ""}})
        tc = acc[tc_index]
        if tc_delta.get("id"):
            tc["id"] = tc_delta["id"]
        fn = tc_delta.get("function", {})
        if fn.get("name"):
            tc["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            tc["function"]["arguments"] += fn["arguments"]
