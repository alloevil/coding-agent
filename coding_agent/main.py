"""
Coding Agent - 主入口

参考 Claude Code 的终端交互体验：
- 权限确认机制
- 流式输出
- 会话管理
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

from .core import AgentLoop, AgentConfig, AgentState, AgentEvent, AgentEventData
from .core.model_client import ModelClient
from .tools import get_registry
from .tools.base import ToolPermission
from .tools.file_ops import register_file_tools
from .tools.shell import register_shell_tools
from .tools.git_ops import register_git_tools
from .tools.browser_ops import register_browser_tools
from .tools.lsp_ops import register_lsp_tools, get_server_manager
from .memory import SessionStore


# 权限级别对应的描述
PERMISSION_LABELS = {
    ToolPermission.READ: "📖 Read",
    ToolPermission.WRITE: "✏️  Write",
    ToolPermission.EXECUTE: "⚡ Execute",
    ToolPermission.DANGEROUS: "⚠️  Dangerous",
}


class CodingAgent:
    """
    Coding Agent 主类
    
    整合所有组件，提供终端交互接口
    """
    
    def __init__(self, config: AgentConfig | None = None):
        self.config = config or AgentConfig.resolve()

        # 初始化工具
        self.tool_registry = get_registry()
        # 写后自动格式化开关
        from .core.formatter import set_enabled as _set_fmt_enabled
        _set_fmt_enabled(getattr(self.config, "auto_format", True))
        # 应用工具执行超时配置（防止挂死调用冻结 agent）
        self.tool_registry.default_tool_timeout = (
            self.config.tool_timeout_seconds
            if self.config.tool_timeout_seconds and self.config.tool_timeout_seconds > 0
            else None
        )
        register_file_tools()
        register_shell_tools()
        register_git_tools()
        register_browser_tools()
        register_lsp_tools()
        from .tools.plan_ops import register_plan_tools
        from .tools.patch_ops import register_patch_tools
        from .tools.tdd_ops import register_tdd_tools
        from .tools.memory_ops import register_memory_tools
        from .tools.web_ops import register_web_tools
        from .tools.ask_ops import register_ask_tools
        from .tools.skill_ops import register_skill_tools
        self.plan_tool = register_plan_tools()
        register_patch_tools()
        register_tdd_tools()
        register_memory_tools()
        register_web_tools()
        self.ask_tool = register_ask_tools(handler=self._ask_user)
        # Skills：注册 skill 工具（渐进式披露的"展开"半步）
        self.skill_tool = register_skill_tools()

        # 配置驱动的命令 hook（settings.json 风格）
        if getattr(self.config, "hooks", None):
            from .core.hooks_config import register_config_hooks
            n = register_config_hooks(self.config.hooks, self.tool_registry)
            if n:
                print(f"   Hooks: registered {n} command hook(s)")

        # 初始化存储
        self.session_store = SessionStore(self.config.session_db_path)

        # 统一模型客户端
        self.model_client = ModelClient(
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            extra_headers=self.config.extra_headers,
        )
        
        # 初始化 Agent Loop
        self.agent_loop = AgentLoop(
            config=self.config,
            tool_registry=self.tool_registry,
            session_store=self.session_store
        )
        
        # 设置模型调用函数
        self.agent_loop.set_model_call_fn(self._call_model)
        # token 预算：让 agent loop 能查询累计 token
        self.agent_loop.set_token_usage_fn(
            lambda: self.model_client.total_prompt_tokens
            + self.model_client.total_completion_tokens
        )
        
        # 设置权限确认回调
        self.agent_loop.set_permission_handler(self._confirm_permission)

        # 外部目录守卫：默认工作区根=当前目录，根外写/执行需确认（除非配置放行）。
        pol = self.agent_loop.permission_policy
        if pol.workspace_root is None:
            import os
            pol.workspace_root = os.getcwd()

        # Skills：把"可用 skills 清单"作为额外 system 块按需注入（渐进式披露）。
        # 用函数延迟求值，使新增/改动 skill 后无需重启即可生效。
        from .core.skills import discover_skills, render_available_skills
        self.agent_loop.set_extra_system_provider(
            lambda: render_available_skills(discover_skills())
        )
        
        # 当前状态
        self.state: AgentState | None = None

        # 流式增量回调（可被前端覆盖）。默认 None → _call_model 用 print。
        # TUI 把它们重定向到 live 缓冲，避免 print 打断 rich.Live 重绘。
        self.on_text_delta: Any = None
        self.on_reasoning_delta: Any = None

    async def _call_model(self, context: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """
        调用模型（支持流式输出）。

        委托给统一的 ModelClient；流式文本通过回调输出。默认打印到终端；
        前端（如 TUI）可通过 self.on_text_delta / on_reasoning_delta 重定向。
        支持 OpenAI 兼容 API（包括小米 mify）。
        """
        if self.on_text_delta is not None:
            on_delta = self.on_text_delta
        elif self.config.stream:
            on_delta = lambda chunk: print(chunk, end="", flush=True)
        else:
            on_delta = None
        # 推理增量：前端覆盖优先；否则流式时用暗色前缀打印，和正文区分
        if self.on_reasoning_delta is not None:
            on_reasoning = self.on_reasoning_delta
        elif self.config.stream:
            on_reasoning = lambda chunk: print(f"\033[2m{chunk}\033[0m", end="", flush=True)
        else:
            on_reasoning = None
        return await self.model_client.complete(
            context,
            tools,
            on_text_delta=on_delta,
            on_reasoning_delta=on_reasoning,
            stream=self.config.stream or self.on_text_delta is not None,
        )

    async def _ask_user(self, question: str, options: list[str]) -> str:
        """ask_user 工具的终端回调：展示问题/选项，读取用户回答。"""
        print(f"\n{'─' * 50}")
        print(f"❓ {question}")
        for i, opt in enumerate(options, 1):
            print(f"   {i}. {opt}")
        print(f"{'─' * 50}")
        try:
            ans = input("   Your answer: ").strip()
        except (EOFError, KeyboardInterrupt):
            return "(no answer)"
        # 允许用序号选择某个选项
        if ans.isdigit() and options and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1]
        return ans or "(no answer)"

    async def _confirm_permission(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """
        权限确认回调
        
        在终端中询问用户是否允许执行
        """
        tool = self.tool_registry.get_tool(tool_name)
        if not tool:
            return False
        
        permission_label = PERMISSION_LABELS.get(tool.permission, tool.permission.value)
        
        print(f"\n{'─' * 50}")
        print(f"🔒 Permission Request: {permission_label}")
        print(f"   Tool: {tool_name}")
        
        # 显示参数（截断过长的值）
        for key, value in arguments.items():
            str_value = str(value)
            if len(str_value) > 200:
                str_value = str_value[:200] + "..."
            print(f"   {key}: {str_value}")
        
        print(f"{'─' * 50}")
        
        # 自动批准模式
        if self.config.auto_approve:
            print("   ✅ Auto-approved")
            return True
        
        # 询问用户
        while True:
            try:
                response = input("   Allow? [y/n/a(llow all)]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            
            if response in ("y", "yes"):
                return True
            elif response in ("n", "no"):
                return False
            elif response in ("a", "all"):
                # 本次会话内自动批准所有
                self.config.auto_approve = True
                print("   ✅ Auto-approve enabled for this session")
                return True
            else:
                print("   Please enter y, n, or a")
    
    async def start(self, session_id: str | None = None) -> None:
        """启动 Agent"""
        # 连接已配置的 MCP servers（失败降级，不阻塞启动）
        self._mcp_clients = []
        if getattr(self.config, "mcp_servers", None):
            from .tools.mcp_client import register_mcp_servers
            try:
                self._mcp_clients = await register_mcp_servers(
                    self.config.mcp_servers, self.tool_registry
                )
                if self._mcp_clients:
                    print(f"   MCP: connected {len(self._mcp_clients)} server(s)")
            except Exception as e:
                print(f"   MCP: failed to connect ({e})")

        # 加载或创建会话
        if session_id:
            self.state = self.session_store.load_state(session_id)
            if not self.state:
                print(f"Session {session_id} not found, creating new one")
                self.state = AgentState(
                    session_id=self.session_store.create_session()
                )
        else:
            self.state = AgentState(
                session_id=self.session_store.create_session()
            )

        # 把计划工具绑定到当前会话状态
        self.plan_tool.bind_state(self.state)
        # 用 session_id 作为 prompt 缓存键，提高稳定前缀的缓存命中率
        self.model_client.prompt_cache_key = self.state.session_id
        # 记录模型名，供 token 估算选对 tokenizer
        self.state.metadata["model"] = self.config.model

        print("🤖 Coding Agent started!")
        print(f"   Session: {self.state.session_id}")
        print(f"   Model: {self.config.model}")
        print(f"   Auto-approve: {'ON' if self.config.auto_approve else 'OFF'}")
        print(f"   Tools: {len(self.tool_registry.get_all_tools())}")
        print()
        print("Commands: /help /tools /cost /compact /new /sessions /quit")
        print("=" * 50)
        
        # 交互循环
        while True:
            try:
                user_input = input("\n💬 You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break
            
            if not user_input:
                continue

            # Slash 命令（/help /tools /cost /compact /new ... + 自定义命令）
            from .core.commands import is_command, dispatch, CommandContext
            # 兼容旧的裸词命令
            if user_input.lower() in ("quit", "exit"):
                user_input = "/quit"
            elif user_input.lower() == "new":
                user_input = "/new"
            elif user_input.lower() == "sessions":
                user_input = "/sessions"
            elif user_input.lower() == "help":
                user_input = "/help"

            if is_command(user_input):
                mc = self.model_client
                ctx = CommandContext(
                    tool_names=[t.name for t in self.tool_registry.get_all_tools()],
                    total_prompt_tokens=mc.total_prompt_tokens,
                    total_completion_tokens=mc.total_completion_tokens,
                    total_reasoning_tokens=mc.total_reasoning_tokens,
                    cache_hit_rate=mc.cache_hit_rate,
                    session_id=self.state.session_id if self.state else None,
                    turn_count=self.state.turn_count if self.state else 0,
                )
                result = dispatch(user_input, ctx)
                handled = await self._handle_command_result(result)
                if handled == "quit":
                    break
                if handled == "continue":
                    continue
                # result.kind == "prompt" -> 落到下面作为用户消息运行
                user_input = result.payload

            # 运行 Agent
            print()

            async for event in self.agent_loop.run(self.state, user_input):
                await self._handle_event(event)

            # 首轮结束后，若还没有标题，生成一个让会话列表可辨识。
            await self._maybe_set_title()

    async def _maybe_set_title(self) -> None:
        """会话尚无标题时，用模型（失败回退启发式）生成一行标题并持久化。"""
        if not self.state:
            return
        if (self.state.metadata or {}).get("title"):
            return
        from .core.session_title import generate_title
        try:
            title = await generate_title(self.state, self._call_model)
        except Exception:
            return
        if title:
            self.state.metadata["title"] = title
            try:
                self.session_store.set_title(self.state.session_id, title)
            except Exception:
                pass

    async def _handle_command_result(self, result) -> str:
        """处理 slash 命令结果。返回 'quit'/'continue'/'run'。"""
        if result.kind == "print":
            print(result.payload)
            return "continue"
        if result.kind == "action":
            act = result.payload
            if act == "quit":
                print("Goodbye!")
                return "quit"
            if act == "new":
                self.state = AgentState(session_id=self.session_store.create_session())
                self.plan_tool.bind_state(self.state)
                self.model_client.prompt_cache_key = self.state.session_id
                print("✨ Started new session")
                return "continue"
            if act == "sessions":
                sessions = self.session_store.list_sessions()
                print("\n📋 Recent sessions:")
                for s in sessions[:5]:
                    title = (s.get("metadata") or {}).get("title", "")
                    label = f" — {title}" if title else ""
                    print(f"   {s['id'][:8]}... ({s['updated_at']}){label}")
                return "continue"
            if act == "plan":
                plan = (self.state.metadata.get("plan") if self.state else None)
                if plan:
                    from .tools.plan_ops import render_plan
                    print(render_plan(plan))
                else:
                    print("No plan set yet.")
                return "continue"
            if act == "compact":
                if self.state:
                    await self.agent_loop.context_manager.compact(
                        self.state, self.agent_loop._model_call_fn)
                    print("📦 Compacted.")
                return "continue"
            if act == "plan_mode":
                pol = self.agent_loop.permission_policy
                was_plan = pol.plan_mode
                pol.plan_mode = not pol.plan_mode
                print("🧭 Plan mode " + ("ON — read-only; the agent can explore and "
                      "plan but won't edit/run." if pol.plan_mode else "OFF — edits allowed again."))
                # plan→build：关闭 plan mode 时注入一次性交接提醒
                if was_plan and not pol.plan_mode and self.state:
                    from .core.agent_handoff import build_switch_note
                    had_plan = bool((self.state.metadata or {}).get("plan"))
                    self.state.metadata["pending_handoff"] = build_switch_note(had_plan)
                return "continue"
            if act.startswith("agent:"):
                self._switch_agent(act.split(":", 1)[1])
                return "continue"
            if act.startswith("model:"):
                self._switch_model(act.split(":", 1)[1])
                return "continue"
            return "continue"
        # "prompt" -> 让调用方把 payload 作为用户消息运行
        return "run"

    def _switch_agent(self, name: str) -> None:
        """切换当前主会话的活动 agent profile：应用其 prompt/model/工具过滤。"""
        from .core.agent_profiles import load_agent
        profile = load_agent(name)
        if profile is None:
            print(f"Agent '{name}' not found. See /agents. "
                  f"Define one at .coding-agent/agents/{name}.md")
            return
        # 应用 system prompt / 模型 / 工具过滤
        if profile.system_prompt:
            self.agent_loop.config.system_prompt = profile.system_prompt
        if profile.model:
            self.config.model = profile.model
            self.model_client.model = profile.model
        if profile.temperature is not None:
            self.model_client.temperature = profile.temperature
        if profile.allow_tools or profile.deny_tools:
            self.agent_loop.set_tool_filter(profile.tool_allowed)
        else:
            self.agent_loop.set_tool_filter(None)
        # 记录活动 agent，供切换交接提醒使用
        prev = (self.state.metadata or {}).get("active_agent") if self.state else None
        if self.state:
            self.state.metadata["active_agent"] = name
            self.state.metadata["prev_agent"] = prev
            # plan→build：从规划态切到执行态时注入一次性交接提醒
            from .core.agent_handoff import should_handoff, build_switch_note
            in_plan_mode = self.agent_loop.permission_policy.plan_mode
            if should_handoff(prev, in_plan_mode, name, in_plan_mode):
                had_plan = bool((self.state.metadata or {}).get("plan"))
                self.state.metadata["pending_handoff"] = build_switch_note(had_plan)
        print(f"🧩 Switched to agent '{name}'"
              + (f" ({profile.model})" if profile.model else "")
              + (f" — {profile.description}" if profile.description else ""))

    def _switch_model(self, spec: str) -> None:
        """切换模型/provider。spec='' 显示当前；'<model>' 仅换模型；
        '<provider>:<model>' 或 '<provider>' 切到配置里的 provider。"""
        providers = getattr(self.config, "providers", {}) or {}
        if not spec:
            cur = self.config.model
            avail = ", ".join(sorted(providers)) if providers else "(none configured)"
            print(f"Current model: {cur}\nProviders: {avail}\n"
                  f"Usage: /model <model> | /model <provider>:<model> | /model <provider>")
            return
        provider_name, _, model_in_spec = spec.partition(":")
        if provider_name in providers:
            p = providers[provider_name]
            base_url = p.get("base_url") or p.get("api_base_url")
            if p.get("api_key"):
                self.model_client.api_key = p["api_key"]
            if base_url:
                self.model_client.base_url = base_url
            if p.get("extra_headers") is not None:
                self.model_client.extra_headers = p["extra_headers"]
            new_model = model_in_spec or p.get("model") or self.config.model
            self.config.model = new_model
            self.model_client.model = new_model
            if self.state:
                self.state.metadata["model"] = new_model
            print(f"🔀 Switched to provider '{provider_name}' (model {new_model})")
            return
        # 不是已知 provider → 当作纯模型名切换（同 provider）
        self.config.model = spec
        self.model_client.model = spec
        if self.state:
            self.state.metadata["model"] = spec
        print(f"🔀 Model set to {spec}")

    
    async def _handle_event(self, event: AgentEventData) -> None:
        """处理 Agent 事件"""
        if event.event == AgentEvent.THINKING:
            print(f"\n💭 Turn {event.data['turn']} - ", end="", flush=True)
        
        elif event.event == AgentEvent.ASSISTANT_MESSAGE:
            # 流式输出已经在 _call_model 中处理
            # 这里只在非流式模式下输出
            content = event.data["content"]
            if content and not self.config.stream:
                print(content)
        
        elif event.event == AgentEvent.TOOL_CALL:
            tool_name = event.data["name"]
            tool_args = event.data["arguments"]
            print(f"\n🔧 {tool_name}")
            for key, value in tool_args.items():
                str_value = str(value)
                if len(str_value) > 150:
                    str_value = str_value[:150] + "..."
                print(f"   {key}: {str_value}")
        
        elif event.event == AgentEvent.TOOL_RESULT:
            result = event.data["result"]
            is_error = event.data["is_error"]
            
            if is_error:
                print(f"   ❌ {result[:300]}")
            else:
                display = result[:300] + "..." if len(result) > 300 else result
                print(f"   ✅ {display}")
        
        elif event.event == AgentEvent.PERMISSION_REQUEST:
            # 权限请求已在 _confirm_permission 中处理
            pass
        
        elif event.event == AgentEvent.ERROR:
            print(f"\n❌ Error: {event.data['error']}")
        
        elif event.event == AgentEvent.COMPACTING:
            print("\n📦 Compacting context...")

        elif event.event == AgentEvent.RETRYING:
            d = event.data
            print(f"\n🔁 Retrying {d.get('tool_name')} "
                  f"(attempt {d.get('attempt')}/{d.get('max_retries')}, "
                  f"waiting {d.get('delay'):.1f}s)...")

        elif event.event == AgentEvent.DONE:
            turns = event.data["turns"]
            print(f"\n{'=' * 50}")
            print(f"✨ Done in {turns} turns")
            mc = self.model_client
            if mc.total_prompt_tokens:
                reasoning = (f", reasoning: {mc.total_reasoning_tokens}"
                             if mc.total_reasoning_tokens else "")
                print(f"   Tokens: {mc.total_prompt_tokens} in / "
                      f"{mc.total_completion_tokens} out{reasoning} "
                      f"(cache hits: {mc.cache_hit_rate*100:.0f}%)")


def _parse_args(argv: list[str]) -> dict[str, Any]:
    """解析 CLI 参数：--resume [id] / --list-sessions / --help。"""
    import argparse
    p = argparse.ArgumentParser(prog="coding-agent", add_help=True,
                                description="Lightweight AI coding agent")
    p.add_argument("--resume", nargs="?", const="__PICK__", default=None,
                   metavar="SESSION_ID",
                   help="Resume a session by id; with no id, pick from recent sessions")
    p.add_argument("--list-sessions", action="store_true",
                   help="List recent sessions and exit")
    p.add_argument("--tui", action="store_true",
                   help="Use the rich TUI front-end")
    args = p.parse_args(argv)
    return {"resume": args.resume, "list_sessions": args.list_sessions, "tui": args.tui}


async def main(argv: list[str] | None = None) -> None:
    """主函数"""
    import sys as _sys
    opts = _parse_args(argv if argv is not None else _sys.argv[1:])

    config = AgentConfig.resolve()

    # --list-sessions: 列出最近会话后退出（不需要 API key）
    if opts["list_sessions"]:
        from .memory import SessionStore
        store = SessionStore(config.session_db_path)
        sessions = store.list_sessions()
        if not sessions:
            print("No sessions yet.")
        else:
            print("Recent sessions:")
            for s in sessions[:20]:
                title = (s.get("metadata") or {}).get("title", "")
                label = f"  {title}" if title else ""
                print(f"  {s['id']}  ({s.get('updated_at', '?')}){label}")
        return

    # 检查 API key
    if not config.api_key:
        print("Error: No API key configured")
        print("Set OPENAI_API_KEY or LLM_API_KEY environment variable")
        sys.exit(1)

    agent = CodingAgent(config)

    # --resume: 恢复指定/选中的会话
    resume_id = opts["resume"]
    if resume_id == "__PICK__":
        sessions = agent.session_store.list_sessions()
        if not sessions:
            print("No sessions to resume; starting fresh.")
            resume_id = None
        else:
            print("Recent sessions:")
            for i, s in enumerate(sessions[:10], 1):
                print(f"  {i}. {s['id'][:8]}... ({s.get('updated_at','?')})")
            try:
                pick = input("Resume which? [number, or Enter for new]: ").strip()
            except (EOFError, KeyboardInterrupt):
                pick = ""
            resume_id = (sessions[int(pick) - 1]["id"]
                         if pick.isdigit() and 1 <= int(pick) <= len(sessions[:10])
                         else None)

    # --tui: 用 rich TUI 前端
    if opts.get("tui"):
        from .ui.app import TuiApp
        await TuiApp(agent).run(session_id=resume_id)
        return

    await agent.start(session_id=resume_id)


def cli() -> None:
    """Synchronous entry point for the `coding-agent` console script."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGoodbye!")


if __name__ == "__main__":
    cli()
