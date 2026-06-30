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

    parts.append(Panel(Text(build_footer(state)), border_style="dim"))
    return Group(*parts)


class TuiApp:
    """rich.Live 驱动的 TUI。复用 CodingAgent 的 agent_loop 与 model_client。"""

    def __init__(self, agent: Any):
        self.agent = agent  # CodingAgent 实例
        self.state = TuiState(
            model=agent.config.model,
        )

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
        elif ev == AgentEvent.ASSISTANT_MESSAGE:
            self.state.add_assistant(data.get("content", ""))
            self.state.status = "idle"
        elif ev == AgentEvent.TOOL_CALL:
            self.state.status = "running_tool"
            self.state.start_tool(data["id"], data["name"], data.get("arguments", {}))
        elif ev == AgentEvent.TOOL_RESULT:
            self.state.finish_tool(data["id"], data.get("result", ""), data.get("is_error", False))
        # 计划从 state.metadata 同步
        if self.agent.state and self.agent.state.metadata.get("plan"):
            self.state.plan = self.agent.state.metadata["plan"]
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
        self.state.session_id = self.agent.state.session_id

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
            with Live(_compose(self.state), console=console, refresh_per_second=8) as live:
                async for event in self.agent.agent_loop.run(self.agent.state, user_input):
                    self._apply_event(event)
                    live.update(_compose(self.state))
