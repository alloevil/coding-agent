"""
会话状态广播 - 结构化的实时进度，供 UI / 外部消费者订阅

参考 opencode 的 session/status.ts：把 agent loop 的事件归约成一个结构化的
SessionStatus（当前状态、正在跑的工具、轮次、累计 token），并在每次变化时
通知订阅者。这让外部（TUI、Web UI、监控、分享）无需理解事件细节就能观察进度。

纯内存、无 IO；agent loop 通过 update_from_event 推进，订阅者通过 subscribe 接收。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Callable


# 状态机取值
IDLE = "idle"
THINKING = "thinking"
RUNNING_TOOL = "running_tool"
COMPACTING = "compacting"
RETRYING = "retrying"
ERROR = "error"
DONE = "done"


@dataclass
class SessionStatus:
    """一次会话的结构化运行状态快照。"""
    session_id: str | None = None
    state: str = IDLE
    turn: int = 0
    current_tool: str | None = None
    last_error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionStatusTracker:
    """把 AgentEvent 归约成 SessionStatus，并在变化时通知订阅者。"""

    def __init__(self, session_id: str | None = None):
        self.status = SessionStatus(session_id=session_id)
        self._subscribers: list[Callable[[SessionStatus], None]] = []

    def subscribe(self, cb: Callable[[SessionStatus], None]) -> Callable[[], None]:
        """注册订阅者，返回取消订阅的函数。"""
        self._subscribers.append(cb)

        def _unsub() -> None:
            try:
                self._subscribers.remove(cb)
            except ValueError:
                pass
        return _unsub

    def _notify(self) -> None:
        for cb in list(self._subscribers):
            try:
                cb(self.status)
            except Exception:
                pass  # 订阅者异常不影响主循环

    def update_from_event(self, event_name: str, data: dict[str, Any] | None = None) -> None:
        """根据一个 AgentEvent（用其 .value 字符串）推进状态。"""
        data = data or {}
        s = self.status
        if event_name == "thinking":
            s.state = THINKING
            s.turn = data.get("turn", s.turn)
            s.current_tool = None
            s.last_error = None
        elif event_name == "tool_call":
            s.state = RUNNING_TOOL
            s.current_tool = data.get("name")
        elif event_name == "tool_result":
            s.current_tool = None
        elif event_name == "assistant_message":
            s.state = IDLE
        elif event_name == "compacting":
            s.state = COMPACTING
        elif event_name == "retrying":
            s.state = RETRYING
        elif event_name == "error":
            s.state = ERROR
            s.last_error = data.get("error")
        elif event_name == "done":
            s.state = DONE
            s.turn = data.get("turns", s.turn)
        else:
            return  # 未知事件不触发通知
        self._notify()

    def set_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """更新累计 token（由 CLI/loop 在每轮后调用）。"""
        self.status.prompt_tokens = prompt_tokens
        self.status.completion_tokens = completion_tokens
        self._notify()

    def render(self) -> str:
        """渲染一行人类可读状态（供 /status 命令）。"""
        s = self.status
        parts = [f"state: {s.state}", f"turn: {s.turn}"]
        if s.current_tool:
            parts.append(f"tool: {s.current_tool}")
        if s.last_error:
            parts.append(f"error: {s.last_error}")
        parts.append(f"tokens: {s.prompt_tokens} in / {s.completion_tokens} out")
        return "  ·  ".join(parts)
