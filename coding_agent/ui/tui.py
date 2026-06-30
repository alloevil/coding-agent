"""
TUI 视图状态与渲染 - 基于 rich

设计原则：把"状态 → renderable"的逻辑做成可单测的纯函数 / 纯状态类
（TuiState + build_* 函数），不依赖终端；真正驱动终端的 TuiApp（rich.Live +
事件循环）放在 app.py，便于测试。

参考 opencode/Claude Code 的 TUI 信息架构：
  - 顶部：会话/模型信息
  - 主体：对话流（用户、助手流式文本、工具调用 + 状态图标、工具结果）
  - 计划面板：当前 plan（常驻）
  - 底部：token/成本 footer
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 工具状态图标
_TOOL_ICONS = {
    "running": "⏳",
    "ok": "✅",
    "error": "❌",
    "denied": "🚫",
}


@dataclass
class ToolCallView:
    """一次工具调用在 UI 里的视图。"""
    id: str
    name: str
    arguments: dict[str, Any]
    status: str = "running"   # running | ok | error | denied
    result: str = ""

    def line(self) -> str:
        icon = _TOOL_ICONS.get(self.status, "•")
        # 参数压成一行预览
        args_preview = ", ".join(
            f"{k}={_short(str(v))}" for k, v in self.arguments.items()
        )
        head = f"{icon} {self.name}({args_preview})"
        if self.status in ("ok", "error") and self.result:
            return head + f"\n    └ {_short(self.result, 200)}"
        return head


@dataclass
class TuiState:
    """TUI 的全部可渲染状态（纯数据）。"""
    model: str = ""
    session_id: str | None = None
    turn: int = 0
    messages: list[dict[str, str]] = field(default_factory=list)  # {role, text}
    tool_calls: list[ToolCallView] = field(default_factory=list)
    plan: list[dict[str, str]] = field(default_factory=list)
    reasoning: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cache_hit_rate: float = 0.0
    status: str = "idle"   # idle | thinking | running_tool | error | compacting | retrying | done
    # 流式缓冲：增量先进这里实时显示，助手消息完成时提交为正式消息。
    live_text: str = ""
    live_reasoning: str = ""
    # 最近一条瞬时提示（错误/压缩/重试），显示在 footer 上方。
    notice: str = ""
    title: str = ""
    plan_mode: bool = False

    # ---- 事件应用 ----
    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "text": text})

    def stream_text(self, chunk: str) -> None:
        """累积流式正文增量。"""
        self.live_text += chunk

    def stream_reasoning(self, chunk: str) -> None:
        """累积流式推理增量。"""
        self.live_reasoning += chunk

    def add_assistant(self, text: str) -> None:
        # 助手消息落定：优先用最终文本，否则用流式缓冲；随后清空缓冲。
        final = text or self.live_text
        if final:
            self.messages.append({"role": "assistant", "text": final})
        self.live_text = ""
        self.live_reasoning = ""

    def start_tool(self, id: str, name: str, arguments: dict[str, Any]) -> None:
        self.tool_calls.append(ToolCallView(id=id, name=name, arguments=arguments))

    def finish_tool(self, id: str, result: str, is_error: bool) -> None:
        for tc in reversed(self.tool_calls):
            if tc.id == id:
                tc.status = "error" if is_error else "ok"
                tc.result = result
                return


def _short(s: str, n: int = 60) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def build_header(state: TuiState) -> str:
    sid = (state.session_id or "")[:8]
    parts = [f"🤖 coding-agent", state.model, f"session {sid}", f"turn {state.turn}"]
    if state.title:
        parts.insert(1, state.title)
    if state.plan_mode:
        parts.append("🧭 plan-mode")
    return "  ·  ".join(parts)


def build_notice(state: TuiState) -> str:
    """瞬时提示（错误/压缩/重试）；无则空串。"""
    return state.notice


def build_plan_panel(state: TuiState) -> str:
    """渲染当前计划（无则返回空串）。"""
    if not state.plan:
        return ""
    sym = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    lines = []
    done = 0
    for s in state.plan:
        if s.get("status") == "completed":
            done += 1
        lines.append(f"{sym.get(s.get('status'), '[ ]')} {s.get('step', '')}")
    lines.append(f"({done}/{len(state.plan)})")
    return "\n".join(lines)


def build_footer(state: TuiState) -> str:
    r = (f" · reasoning {state.reasoning_tokens}"
         if state.reasoning_tokens else "")
    cache = (f" · cache {state.cache_hit_rate*100:.0f}%"
             if state.prompt_tokens else "")
    status = {"idle": "ready", "thinking": "thinking…",
              "running_tool": "running tool…", "error": "error",
              "compacting": "compacting…", "retrying": "retrying…",
              "done": "done"}.get(state.status, state.status)
    return (f"[{status}]  {state.prompt_tokens} in / "
            f"{state.completion_tokens} out{r}{cache}")


def build_transcript(state: TuiState, max_messages: int = 12) -> str:
    """把最近若干条消息渲染成文本（纯函数，便于测试）。

    若有正在流式输出的 live_text，作为最后一条进行中的 Agent 行追加显示。
    """
    lines = []
    for m in state.messages[-max_messages:]:
        prefix = "You: " if m["role"] == "user" else "Agent: "
        lines.append(prefix + m["text"])
    if state.live_text:
        lines.append("Agent: " + state.live_text + " ▌")
    return "\n\n".join(lines)
