"""
TuiApp - 用 rich.Live 驱动的 TUI 前端

把 AgentLoop 的事件流映射到 TuiState，并实时重绘。渲染用 rich，但所有状态
变更走 tui.py 里的纯逻辑，方便单测；这里只负责 IO 与布局组装。
"""
from __future__ import annotations

from typing import Any

from .tui import (
    TuiState, build_header, build_footer, build_plan_panel, build_transcript,
)


def _compose(state: TuiState):
    """把 TuiState 组装成一个 rich renderable（懒导入 rich，便于无终端测试）。"""
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    parts = []
    parts.append(Panel(Text(build_header(state)), style="bold cyan"))

    transcript = build_transcript(state)
    if transcript:
        parts.append(Panel(Text(transcript), title="conversation", border_style="dim"))

    if state.tool_calls:
        tool_text = "\n".join(tc.line() for tc in state.tool_calls[-8:])
        parts.append(Panel(Text(tool_text), title="tools", border_style="yellow"))

    plan = build_plan_panel(state)
    if plan:
        parts.append(Panel(Text(plan), title="plan", border_style="green"))

    if state.notice:
        parts.append(Panel(Text(state.notice), border_style="red"))

    parts.append(Panel(Text(build_footer(state)), border_style="dim"))
    return Group(*parts)


class TuiApp:
    """rich.Live 驱动的 TUI。复用 CodingAgent 的 agent_loop 与 model_client。"""

    def __init__(self, agent: Any):
        self.agent = agent  # CodingAgent 实例
        self.state = TuiState(
            model=agent.config.model,
        )
        self._live: Any = None  # 当前 rich.Live，供流式回调刷新

    def _on_text_delta(self, chunk: str) -> None:
        """流式正文增量 → live 缓冲 + 刷新（替代 print，避免打断 Live）。"""
        self.state.stream_text(chunk)
        self._refresh()

    def _on_reasoning_delta(self, chunk: str) -> None:
        self.state.stream_reasoning(chunk)
        self._refresh()

    def _refresh(self) -> None:
        if self._live is not None:
            try:
                self._live.update(_compose(self.state))
            except Exception:
                pass

    def _suspend_live(self):
        """上下文管理器：在交互式提问期间暂停 Live，避免与 input() 抢终端。"""
        app = self

        class _Suspend:
            def __enter__(self_inner):
                if app._live is not None:
                    try:
                        app._live.stop()
                    except Exception:
                        pass
                return self_inner

            def __exit__(self_inner, *exc):
                if app._live is not None:
                    try:
                        app._live.start(refresh=True)
                    except Exception:
                        pass
                return False

        return _Suspend()

    async def _confirm_permission(self, tool_name: str, arguments: dict) -> bool:
        """TUI 权限确认：暂停 Live 后委托给 CodingAgent 的终端实现。"""
        with self._suspend_live():
            return await self.agent._confirm_permission(tool_name, arguments)

    async def _ask_user(self, question: str, options: list) -> str:
        """TUI ask_user：暂停 Live 后委托给 CodingAgent 的终端实现。"""
        with self._suspend_live():
            return await self.agent._ask_user(question, options)

    def _sync_usage(self) -> None:
        mc = self.agent.model_client
        self.state.prompt_tokens = mc.total_prompt_tokens
        self.state.completion_tokens = mc.total_completion_tokens
        self.state.reasoning_tokens = mc.total_reasoning_tokens
        self.state.cache_hit_rate = mc.cache_hit_rate

    def _apply_event(self, event: Any) -> None:
        """把一个 AgentEventData 映射到 state。"""
        from ..core import AgentEvent
        ev, data = event.event, event.data
        if ev == AgentEvent.THINKING:
            self.state.status = "thinking"
            self.state.turn = data.get("turn", self.state.turn)
            self.state.notice = ""
        elif ev == AgentEvent.ASSISTANT_MESSAGE:
            self.state.add_assistant(data.get("content", ""))
            self.state.status = "idle"
        elif ev == AgentEvent.TOOL_CALL:
            self.state.status = "running_tool"
            self.state.start_tool(data["id"], data["name"], data.get("arguments", {}))
        elif ev == AgentEvent.TOOL_RESULT:
            self.state.finish_tool(data["id"], data.get("result", ""), data.get("is_error", False))
        elif ev == AgentEvent.ERROR:
            self.state.status = "error"
            self.state.notice = f"❌ {data.get('error', 'error')}"
        elif ev == AgentEvent.COMPACTING:
            self.state.status = "compacting"
            self.state.notice = "📦 compacting context…"
        elif ev == AgentEvent.RETRYING:
            self.state.status = "retrying"
            self.state.notice = (
                f"🔁 retrying {data.get('tool_name', '')} "
                f"(attempt {data.get('attempt', '?')}/{data.get('max_retries', '?')})"
            )
        elif ev == AgentEvent.DONE:
            self.state.status = "done"
            self.state.turn = data.get("turns", self.state.turn)
        # 计划从 state.metadata 同步
        if self.agent.state and self.agent.state.metadata.get("plan"):
            self.state.plan = self.agent.state.metadata["plan"]
        # 标题 / plan-mode 同步
        if self.agent.state:
            self.state.title = (self.agent.state.metadata or {}).get("title", self.state.title)
        try:
            self.state.plan_mode = self.agent.agent_loop.permission_policy.plan_mode
        except Exception:
            pass
        self._sync_usage()

    async def run(self, session_id: str | None = None) -> None:
        """启动 TUI 主循环。"""
        from rich.console import Console
        from rich.live import Live
        from ..core import AgentState

        console = Console()
        # 会话初始化（复用 agent 的 store）
        if session_id:
            self.agent.state = self.agent.session_store.load_state(session_id) \
                or AgentState(session_id=self.agent.session_store.create_session())
        else:
            self.agent.state = AgentState(session_id=self.agent.session_store.create_session())
        self.agent.plan_tool.bind_state(self.agent.state)
        self.agent.model_client.prompt_cache_key = self.agent.state.session_id
        self.agent.state.metadata["model"] = self.agent.config.model
        self.state.session_id = self.agent.state.session_id

        # 把流式增量重定向到 live 缓冲（替代 print），修复 print 打断 Live 的问题。
        self.agent.on_text_delta = self._on_text_delta
        self.agent.on_reasoning_delta = self._on_reasoning_delta

        # 权限确认 / ask_user 在提问时暂停 Live，避免和 input() 抢终端。
        self.agent.agent_loop.set_permission_handler(self._confirm_permission)
        if getattr(self.agent, "ask_tool", None) is not None:
            self.agent.ask_tool.set_handler(self._ask_user)

        console.print(_compose(self.state))
        console.print("Type a message, or /help. Ctrl-C to quit.")

        while True:
            try:
                user_input = console.input("\n[bold green]›[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\nGoodbye!")
                break
            if not user_input:
                continue

            # slash 命令复用 CLI 的处理
            from ..core.commands import is_command, dispatch, CommandContext
            if is_command(user_input):
                ctx = CommandContext(
                    tool_names=[t.name for t in self.agent.tool_registry.get_all_tools()],
                    total_prompt_tokens=self.agent.model_client.total_prompt_tokens,
                    total_completion_tokens=self.agent.model_client.total_completion_tokens,
                    total_reasoning_tokens=self.agent.model_client.total_reasoning_tokens,
                    cache_hit_rate=self.agent.model_client.cache_hit_rate,
                    session_id=self.agent.state.session_id,
                    turn_count=self.agent.state.turn_count,
                )
                result = dispatch(user_input, ctx)
                handled = await self.agent._handle_command_result(result)
                if handled == "quit":
                    break
                if handled == "continue":
                    continue
                user_input = result.payload

            self.state.add_user(user_input)
            with Live(_compose(self.state), console=console,
                      refresh_per_second=8) as live:
                self._live = live
                try:
                    async for event in self.agent.agent_loop.run(self.agent.state, user_input):
                        self._apply_event(event)
                        live.update(_compose(self.state))
                finally:
                    self._live = None
