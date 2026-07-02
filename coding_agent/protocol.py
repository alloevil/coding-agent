"""
Agent Protocol - JSON stdin/stdout 协议

Go TUI 通过 subprocess 启动 agent，通过 stdin/stdout JSON 通信。

协议格式：
- 请求：{"type": "user_input", "content": "...", "session_id": "..."}
- 响应：{"type": "event", "event": "...", "data": {...}}

事件类型：
- thinking: 模型思考中
- assistant_message: 助手消息（流式）
- tool_call: 工具调用
- tool_result: 工具结果
- permission_request: 权限确认请求
- error: 错误
- done: 完成
- compacting: 上下文压缩
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from .core import AgentLoop, AgentConfig, AgentState, AgentEvent, AgentEventData
from .core.model_client import ModelClient
from .tools import get_registry
from .tools.file_ops import register_file_tools
from .tools.shell import register_shell_tools
from .tools.git_ops import register_git_tools
from .memory import SessionStore


class AgentProtocol:
    """
    JSON 协议处理器
    
    通过 stdin 接收请求，通过 stdout 发送事件。
    Go TUI 只需要 subprocess 启动这个进程，然后读写 stdin/stdout。
    """
    
    def __init__(self, config: AgentConfig):
        self.config = config
        # Anthropic 协议下较新的 Claude 模型（Opus 4.8 等，Bedrock 承载）
        # 已弃用 temperature，显式传会 400。除非用户显式设过，否则省略。
        if getattr(config, "protocol", "openai") == "anthropic":
            config.temperature = None
        
        # 初始化工具
        register_file_tools()
        register_shell_tools()
        register_git_tools()
        from .tools.plan_ops import register_plan_tools
        from .tools.patch_ops import register_patch_tools
        from .tools.tdd_ops import register_tdd_tools
        from .tools.memory_ops import register_memory_tools
        from .tools.web_ops import register_web_tools
        from .tools.ask_ops import register_ask_tools
        self.plan_tool = register_plan_tools()
        register_patch_tools()
        register_tdd_tools()
        register_memory_tools()
        register_web_tools()
        self.ask_tool = register_ask_tools(handler=self._ask_user)
        
        self.tool_registry = get_registry()
        self.session_store = SessionStore(config.session_db_path)
        self.agent_loop = AgentLoop(
            config=config,
            tool_registry=self.tool_registry,
            session_store=self.session_store
        )

        # 统一模型客户端
        self.model_client = ModelClient(
            api_key=config.api_key,
            base_url=config.api_base_url,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            extra_headers=getattr(config, "extra_headers", None),
            protocol=getattr(config, "protocol", "openai"),
            thinking_budget=getattr(config, "thinking_budget", 0),
        )
        
        # 设置模型调用
        self.agent_loop.set_model_call_fn(self._call_model)
        self.agent_loop.set_token_usage_fn(
            lambda: self.model_client.total_prompt_tokens
            + self.model_client.total_completion_tokens
        )
        
        # 设置权限确认
        self.agent_loop.set_permission_handler(self._confirm_permission)

        # Skills + 项目记忆按需注入（渐进式披露），与 CLI 前端一致。
        # 之前 TUI 后端没设这个 provider → skills/记忆都进不了上下文。
        from .core.skills import discover_skills, render_available_skills
        from .memory.project import ProjectMemoryManager

        def _extra_system() -> str:
            blocks: list[str] = []
            skills = render_available_skills(discover_skills())
            if skills:
                blocks.append(skills)
            try:
                mem = ProjectMemoryManager(".").get_context_for_agent()
            except Exception:
                mem = ""
            if mem:
                blocks.append(mem)
            return "\n\n".join(blocks)

        self.agent_loop.set_extra_system_provider(_extra_system)

        # 当前状态
        self.state: AgentState | None = None
        # 当前正在运行的 turn 任务（None 表示空闲）
        self._turn_task: asyncio.Task | None = None
        
        # 权限确认队列（等待 TUI 响应）
        self._permission_event = asyncio.Event()
        self._permission_result: bool = False

        # ask_user 问答队列（等待 TUI 响应）
        self._question_event = asyncio.Event()
        self._question_answer: str = ""

    async def _ask_user(self, question: str, options: list[str]) -> str:
        """ask_user 工具：发问题事件给 TUI，等待 question_response。"""
        self._send_event("question", {"question": question, "options": options})
        self._question_event.clear()
        await self._question_event.wait()
        return self._question_answer

    async def _call_model(self, context: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """调用模型（流式）。委托给统一 ModelClient；正文增量转为 stream_text 事件，
        推理增量转为 stream_reasoning 事件（供 TUI 显示思考过程）。

        _suppress_stream=True 时（如 /compact 的内部总结调用）不外发增量事件，
        避免内部操作的模型输出串进 TUI transcript。"""
        suppress = getattr(self, "_suppress_stream", False)
        return await self.model_client.complete(
            context,
            tools,
            on_text_delta=(None if suppress
                           else lambda chunk: self._send_event("stream_text", {"text": chunk})),
            on_reasoning_delta=(None if suppress
                                else lambda chunk: self._send_event("stream_reasoning", {"text": chunk})),
            stream=True,
        )
    
    async def _confirm_permission(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """权限确认：发送请求给 TUI，等待响应"""
        if self.config.auto_approve:
            return True
        
        # 发送权限请求
        self._send_event("permission_request", {
            "tool_name": tool_name,
            "arguments": arguments
        })
        
        # 等待 TUI 响应
        self._permission_event.clear()
        await self._permission_event.wait()
        
        return self._permission_result
    
    def _send_event(self, event_type: str, data: dict[str, Any]) -> None:
        """发送事件到 stdout"""
        msg = json.dumps({"type": event_type, **data}, ensure_ascii=False)
        print(msg, flush=True)
    
    async def handle_request(self, request: dict[str, Any]) -> None:
        """处理来自 TUI 的请求"""
        req_type = request.get("type")
        
        if req_type == "user_input":
            content = request.get("content", "")
            session_id = request.get("session_id")

            # 加载或创建会话
            if session_id:
                self.state = self.session_store.load_state(session_id)
            if not self.state:
                self.state = AgentState(
                    session_id=self.session_store.create_session()
                )

            # 把计划工具绑定到当前会话状态
            self.plan_tool.bind_state(self.state)

            # Slash 命令（/model /config /help /tools /status ...）：在后端拦截并
            # 分发，而不是喂给 LLM。结果作为 command_result 事件发回 TUI。
            from .core.commands import is_command
            if is_command(content):
                await self._handle_slash_command(content)
            elif content.startswith("!") and len(content) > 1:
                # `!command` 直通（Claude Code 风格）：直接跑 shell，不经 LLM。
                # 输出回 TUI 显示，并记入会话上下文供模型后续参考。
                self._turn_task = asyncio.ensure_future(
                    self._run_bang_shell(content[1:].strip()))
            else:
                # @file 引用：把 "@path" 展开成一段带文件内容的上下文附注，
                # 让模型看到实际内容（Claude Code 行为），而不是字面量 "@path"。
                content = self._expand_file_mentions(content)
                # 作为独立任务运行 turn，使 handle_request 立即返回、
                # dispatcher 能继续读取后续请求（如运行期间的 interrupt）。
                self._turn_task = asyncio.ensure_future(self._run_turn(content))

        elif req_type == "permission_response":
            self._permission_result = request.get("approved", False)
            self._permission_event.set()

        elif req_type == "question_response":
            self._question_answer = request.get("answer", "")
            self._question_event.set()

        elif req_type == "new_session":
            self.state = AgentState(
                session_id=self.session_store.create_session()
            )
            self._send_event("session_created", {
                "session_id": self.state.session_id
            })
        
        elif req_type == "list_sessions":
            sessions = self.session_store.list_sessions()
            self._send_event("sessions_list", {"sessions": sessions})
        
        elif req_type == "set_auto_approve":
            self.config.auto_approve = request.get("value", False)
            self._send_event("config_updated", {
                "auto_approve": self.config.auto_approve
            })
        
        elif req_type == "interrupt":
            self.agent_loop.interrupt()
            self._send_event("interrupted", {
                "message": "Interrupt signal sent"
            })

        elif req_type == "rewind":
            # Esc-Esc：回退最后一轮。弹出尾部消息直到（含）最后一个 user 消息，
            # 把该消息文本发回（rewound 事件）供前端填回输入框编辑重发。
            from .core.state import MessageRole
            popped_text = ""
            if self.state is not None and self.state.messages:
                msgs = self.state.messages
                # 找最后一个 user 消息的位置
                idx = None
                for i in range(len(msgs) - 1, -1, -1):
                    if msgs[i].role == MessageRole.USER:
                        idx = i
                        break
                if idx is not None:
                    popped_text = msgs[idx].content or ""
                    del msgs[idx:]
                    # turn 数同步回退一轮（不为负）
                    self.state.turn_count = max(0, self.state.turn_count - 1)
            self._send_event("rewound", {"text": popped_text})

        elif req_type == "save_config":
            # 引导式配置：前端把答案发来，写入全局 config.json 并热更当前 client。
            from .core.setup_wizard import write_config
            answers = request.get("answers", {})
            try:
                path = write_config(answers)
                # 热更新当前会话的模型客户端（无需重启即可用新配置）
                from .core.config import AgentConfig
                new_cfg = AgentConfig.resolve()
                self.config.api_key = new_cfg.api_key
                self.config.model = new_cfg.model
                self.config.api_base_url = new_cfg.api_base_url
                self.config.protocol = getattr(new_cfg, "protocol", "openai")
                self.model_client.api_key = new_cfg.api_key
                self.model_client.model = new_cfg.model
                self.model_client.base_url = new_cfg.api_base_url.rstrip("/")
                self.model_client.protocol = getattr(new_cfg, "protocol", "openai")
                self.model_client.extra_headers = getattr(new_cfg, "extra_headers", {}) or {}
                self._send_event("config_saved", {"path": str(path),
                                                  "model": new_cfg.model})
            except Exception as e:  # noqa: BLE001
                self._send_event("error", {"error": f"save_config failed: {e}"})
    
    async def _run_turn(self, content: str) -> None:
        """运行一个 turn 并转发事件；结束发 session_state。作为独立任务运行，
        使 stdin 读取不被阻塞（运行期间可处理 interrupt）。"""
        try:
            async for event in self.agent_loop.run(self.state, content):
                self._forward_event(event)
        except Exception as e:  # noqa: BLE001
            self._send_event("error", {"error": str(e)})
        finally:
            if self.state is not None:
                mc = getattr(self, "model_client", None)
                ev: dict[str, Any] = {
                    "session_id": self.state.session_id,
                    "turn_count": self.state.turn_count,
                }
                if mc is not None:
                    ev["prompt_tokens"] = mc.total_prompt_tokens
                    ev["completion_tokens"] = mc.total_completion_tokens
                    ev["max_context_tokens"] = getattr(self.config, "max_context_tokens", 0)
                    # 美元估算（未知模型/无覆盖时省略字段，前端就不显示）
                    from .core.pricing import estimate_cost
                    cost = estimate_cost(self.config.model, mc.total_prompt_tokens,
                                         mc.total_completion_tokens,
                                         override=getattr(self.config, "pricing", None))
                    if cost is not None:
                        ev["cost_usd"] = round(cost, 4)
                self._send_event("session_state", ev)
            self._turn_task = None

    def _expand_file_mentions(self, content: str) -> str:
        """
        把消息里的 `@path` 引用展开：在原文后附上被引文件的内容，让模型直接看到
        （Claude Code 行为）。只展开工作区内、真实存在、非二进制、体积合理的文件；
        找不到就原样保留 `@path`（不报错）。最多展开 5 个，每个截断到 ~16KB。
        """
        import re
        from pathlib import Path
        # @ 前是行首或空白；path 允许字母数字/._/-，不含空白
        mentions = re.findall(r"(?:^|\s)@([\w./\-]+)", content)
        if not mentions:
            return content
        root = Path.cwd().resolve()
        blocks: list[str] = []
        seen: set[str] = set()
        for rel in mentions:
            if rel in seen or len(seen) >= 5:
                continue
            seen.add(rel)
            try:
                p = (root / rel).resolve()
                p.relative_to(root)  # 越界即抛 → 跳过
                if not p.is_file() or p.stat().st_size > 2_000_000:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
                if "\x00" in text[:1024]:  # 疑似二进制
                    continue
                if len(text) > 16_000:
                    text = text[:16_000] + "\n… (truncated)"
                blocks.append(f"\n\n--- {rel} ---\n{text}")
            except (OSError, ValueError):
                continue
        return content + "".join(blocks) if blocks else content

    async def _run_bang_shell(self, command: str) -> None:
        """`!command` 直通：用注册的 shell 工具直接执行（沙箱+超时），输出发回
        TUI（shell_output 事件），并把命令+输出记入会话上下文供模型后续参考。"""
        try:
            tool = self.tool_registry.get_tool("shell_exec")
            if tool is None:
                self._send_event("shell_output", {
                    "command": command, "output": "shell_exec tool unavailable"})
                return
            output = await tool.execute(command=command)
            self._send_event("shell_output", {"command": command,
                                              "output": str(output)[:8000]})
            # 记入上下文：模型下轮能看到用户手动跑了什么、结果如何。
            if self.state is not None:
                self.state.add_user_message(
                    f"[ran shell command] $ {command}\n{str(output)[:4000]}")
        except Exception as e:  # noqa: BLE001
            self._send_event("shell_output", {"command": command,
                                              "output": f"error: {e}"})
        finally:
            self._turn_task = None

    async def _handle_slash_command(self, text: str) -> None:
        """分发 slash 命令并把结果作为事件发回 TUI（不喂给 LLM）。"""
        from .core.commands import dispatch, CommandContext
        mc = self.model_client
        ctx = CommandContext(
            tool_names=[t.name for t in self.tool_registry.get_all_tools()],
            total_prompt_tokens=mc.total_prompt_tokens,
            total_completion_tokens=mc.total_completion_tokens,
            total_reasoning_tokens=mc.total_reasoning_tokens,
            cache_hit_rate=mc.cache_hit_rate,
            session_id=self.state.session_id if self.state else None,
            turn_count=self.state.turn_count if self.state else 0,
            model=self.config.model,
            pricing=getattr(self.config, "pricing", None),
        )
        try:
            result = dispatch(text, ctx)
        except Exception as e:  # noqa: BLE001
            self._send_event("command_result", {"text": f"command error: {e}"})
            return

        if result.kind == "print":
            self._send_event("command_result", {"text": result.payload})
        elif result.kind == "prompt":
            # 命令展开成一段提示（如自定义命令）→ 作为一次 turn 运行。
            self._turn_task = asyncio.ensure_future(self._run_turn(result.payload))
        elif result.kind == "action":
            await self._handle_command_action(result.payload)

    async def _handle_command_action(self, action: str) -> None:
        """处理命令 action（/model 切换、/new、/compact 等），发事件反馈 TUI。"""
        if action.startswith("model:"):
            spec = action.split(":", 1)[1].strip()
            if not spec:
                self._send_event("command_result", {
                    "text": f"Current model: {self.config.model} "
                            f"({getattr(self.config, 'protocol', 'openai')})"})
                return
            # /model <model> 或 /model <provider>:<model>：热更当前客户端。
            self.config.model = spec
            self.model_client.model = spec
            self._send_event("command_result", {"text": f"🔀 Model → {spec}"})
            # 更新 header 上的模型名
            self._send_event("model_changed", {"model": spec})
        elif action == "new":
            self.state = AgentState(session_id=self.session_store.create_session())
            self.plan_tool.bind_state(self.state)
            self._send_event("command_result", {"text": "✨ Started a new session"})
        elif action == "compact":
            if self.state is not None:
                before = self.state.get_token_estimate()
                # 内部总结不应串进 transcript：临时静音流式增量。
                self._suppress_stream = True
                try:
                    await self.agent_loop.context_manager.compact(self.state, self._call_model)
                finally:
                    self._suppress_stream = False
                after = self.state.get_token_estimate()
                if after < before:
                    self._send_event("command_result", {
                        "text": f"🗜️  Context compacted (~{before} → ~{after} tokens)"})
                else:
                    self._send_event("command_result", {
                        "text": "🗜️  Context already compact — nothing to reclaim."})
            else:
                self._send_event("command_result", {"text": "Nothing to compact yet"})
        elif action == "quit":
            # /quit /exit：告诉前端退出（后端无法直接关 TUI）。
            self._send_event("quit", {})
        elif action == "sessions":
            # /sessions /resume：列会话给前端（TUI 会开选择器）。
            sessions = self.session_store.list_sessions()
            self._send_event("sessions_list", {"sessions": sessions})
        elif action == "diff":
            # /diff：显示工作区改动（git diff --stat + diff）。
            try:
                tool = self.tool_registry.get_tool("git_diff")
                out = await tool.execute() if tool else "git_diff unavailable"
            except Exception as e:  # noqa: BLE001
                out = f"diff failed: {e}"
            self._send_event("command_result", {"text": str(out)[:8000] or "No changes."})
        elif action.startswith("memory:"):
            # /memory 显示项目记忆；/memory add <text> 存一条知识。
            from .memory.project import ProjectMemoryManager
            spec = action.split(":", 1)[1].strip()
            mgr = ProjectMemoryManager(".")
            if spec.startswith("add ") and spec[4:].strip():
                mgr.save_knowledge(spec[4:].strip(), source="/memory")
                self._send_event("command_result", {"text": "🧠 Saved to project memory."})
            else:
                mem = mgr.get_context_for_agent()
                self._send_event("command_result",
                                 {"text": mem or "No project memory yet. "
                                                 "Add with: /memory add <text>"})
        elif action.startswith("export:"):
            # /export [path]：把当前会话转写导出到 markdown 文件。
            self._handle_export(action.split(":", 1)[1].strip())
        elif action == "undo":
            # /undo：恢复最近一次文件改动（编辑日志）。
            from .core.edit_journal import get_edit_journal
            self._send_event("command_result", {"text": get_edit_journal().undo_last()})
        elif action == "mcp":
            # /mcp：列出配置的 MCP servers（+ 是否已连接）。
            servers = getattr(self.config, "mcp_servers", None) or {}
            if not servers:
                text = "No MCP servers configured. Add them under \"mcp_servers\" in config.json."
            else:
                connected = len(getattr(self, "_mcp_clients", []) or [])
                lines = [f"MCP servers ({len(servers)} configured, {connected} connected):"]
                for name, cfg in servers.items():
                    where = cfg.get("url") or " ".join(cfg.get("command", [])) or "?"
                    lines.append(f"  • {name} — {where}")
                text = "\n".join(lines)
            self._send_event("command_result", {"text": text})
        elif action == "hooks":
            # /hooks：列出配置的生命周期 hooks。
            hooks = getattr(self.config, "hooks", None) or {}
            if not hooks:
                text = "No hooks configured. Add them under \"hooks\" in config.json."
            else:
                lines = ["Configured hooks:"]
                for event, items in hooks.items():
                    n = len(items) if isinstance(items, list) else 1
                    lines.append(f"  • {event}: {n} command(s)")
                text = "\n".join(lines)
            self._send_event("command_result", {"text": text})
        elif action == "doctor" or action == "doctor:probe":
            # /doctor：环境自检（静态）；/doctor probe 额外真实探测端点。
            from .core import doctor as D
            if action == "doctor:probe":
                report = await D.run_full(self.config)
            else:
                report = D.run_static(self.config)
            self._send_event("command_result", {"text": report.render()})
        elif action == "permissions" or action.startswith("permissions:"):
            # /permissions：显示/切换审批模式。auto=自动放行；ask=逐次确认。
            spec = action.split(":", 1)[1].strip() if ":" in action else ""
            if spec in ("auto", "ask"):
                self.config.auto_approve = (spec == "auto")
                try:
                    from .core.setup_wizard import set_config_value
                    set_config_value("auto_approve",
                                     "true" if self.config.auto_approve else "false")
                    saved = " (saved)"
                except Exception:  # noqa: BLE001 — 切换成功即可，持久化失败不致命
                    saved = " (not persisted)"
                self._send_event("config_updated",
                                 {"auto_approve": self.config.auto_approve})
                self._send_event("command_result", {
                    "text": f"🔓 Approval mode → {spec}"
                            f" ({'auto-approving tools' if spec == 'auto' else 'confirming each write/exec'})"
                            f"{saved}"})
            else:
                mode = "auto" if self.config.auto_approve else "ask"
                self._send_event("command_result", {
                    "text": f"Approval mode: {mode}"
                            f" ({'tools run without confirmation' if mode == 'auto' else 'each write/exec is confirmed'})."
                            f"\nChange with: /permissions auto|ask"})
        elif action == "status":
            # /status：会话结构化状态（会话/回合/token 用量）。
            mc = self.model_client
            st = self.state
            sid = (getattr(st, "session_id", "") or "")[:8]
            turns = getattr(st, "turn_count", 0) if st else 0
            nmsg = len(getattr(st, "messages", []) or []) if st else 0
            pol = getattr(self.agent_loop, "permission_policy", None)
            plan_mode = getattr(pol, "plan_mode", False)
            lines = [
                "📊 Session status",
                f"  session: {sid or '(none)'}   turn: {turns}   messages: {nmsg}",
                f"  model: {getattr(self.config, 'model', '?')} "
                f"({getattr(self.config, 'protocol', 'openai')})",
                f"  tokens: {mc.total_prompt_tokens} in / {mc.total_completion_tokens} out"
                + (f" / {mc.total_reasoning_tokens} reasoning" if getattr(mc, 'total_reasoning_tokens', 0) else "")
                + f"   cache {mc.cache_hit_rate*100:.0f}%",
                f"  approval: {'auto' if self.config.auto_approve else 'ask'}"
                + ("   🧭 plan-mode ON" if plan_mode else ""),
            ]
            self._send_event("command_result", {"text": "\n".join(lines)})
        elif action == "plan":
            # /plan：渲染当前计划（若有）。
            plan = (self.state.metadata.get("plan")
                    if self.state and getattr(self.state, "metadata", None) else None)
            if plan:
                from .tools.plan_ops import render_plan
                self._send_event("command_result", {"text": render_plan(plan)})
            else:
                self._send_event("command_result", {"text": "No plan set yet."})
        elif action == "plan_mode":
            # /plan-mode：切换只读规划模式；关闭时注入一次性 plan→build 交接提醒。
            pol = getattr(self.agent_loop, "permission_policy", None)
            if pol is None:
                self._send_event("command_result", {"text": "Plan mode unavailable."})
            else:
                was_plan = pol.plan_mode
                pol.plan_mode = not pol.plan_mode
                msg = ("🧭 Plan mode ON — read-only; the agent can explore and plan "
                       "but won't edit/run." if pol.plan_mode
                       else "🧭 Plan mode OFF — edits allowed again.")
                if was_plan and not pol.plan_mode and self.state:
                    from .core.agent_handoff import build_switch_note
                    had_plan = bool((self.state.metadata or {}).get("plan"))
                    self.state.metadata["pending_handoff"] = build_switch_note(had_plan)
                self._send_event("command_result", {"text": msg})
        elif action == "config" or action.startswith("config:"):
            # /config：显示（打码后的）全局配置。改配置走 CLI 或 /setup。
            from .core import setup_wizard as W
            import json as _json
            spec = action.split(":", 1)[1].strip() if ":" in action else ""
            if not spec:
                shown = _json.dumps(W.redact(W.read_config()), indent=2, ensure_ascii=False)
                self._send_event("command_result", {"text": shown or "(no config)"})
            else:
                self._send_event("command_result", {
                    "text": "Editing config from the TUI isn't supported — use "
                            "/setup to reconfigure, or `coding-agent config set <k> <v>`."})
        elif action.startswith("agent:"):
            # /agent <name>：切换活动 agent profile（prompt/model/工具过滤）。
            name = action.split(":", 1)[1].strip()
            self._switch_agent_profile(name)
        elif action == "setup":
            # /setup：请前端重开配置向导。向导保存后走 save_config 热更配置。
            self._send_event("open_setup", {})
            self._send_event("command_result", {"text": "🧩 Opening setup…"})
        else:
            # 其它 action（setup...）：给一个可读回执。
            self._send_event("command_result", {"text": f"({action})"})

    def _switch_agent_profile(self, name: str) -> None:
        """切换当前会话的活动 agent profile（对齐 CLI._switch_agent）。"""
        from .core.agent_profiles import load_agent
        profile = load_agent(name)
        if profile is None:
            self._send_event("command_result", {
                "text": f"Agent '{name}' not found. See /agents. "
                        f"Define one at .coding-agent/agents/{name}.md"})
            return
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
        prev = (self.state.metadata or {}).get("active_agent") if self.state else None
        if self.state:
            self.state.metadata["active_agent"] = name
            self.state.metadata["prev_agent"] = prev
            from .core.agent_handoff import should_handoff, build_switch_note
            in_plan_mode = getattr(
                getattr(self.agent_loop, "permission_policy", None), "plan_mode", False)
            if should_handoff(prev, in_plan_mode, name, in_plan_mode):
                had_plan = bool((self.state.metadata or {}).get("plan"))
                self.state.metadata["pending_handoff"] = build_switch_note(had_plan)
        # 模型可能已换 → 同步 header
        if profile.model:
            self._send_event("model_changed", {"model": profile.model})
        self._send_event("command_result", {
            "text": f"🧩 Switched to agent '{name}'"
                    + (f" ({profile.model})" if profile.model else "")
                    + (f" — {profile.description}" if profile.description else "")})

    def _handle_export(self, path_arg: str) -> None:
        """把当前会话转写导出为 markdown。默认写到 cwd 下带会话 id 的文件。"""
        from .core.state import MessageRole
        if self.state is None or not self.state.messages:
            self._send_event("command_result", {"text": "Nothing to export yet."})
            return
        sid = self.state.session_id or "session"
        path = path_arg or f"coding-agent-{sid[:8]}.md"
        lines = [f"# coding-agent session {sid}", ""]
        for m in self.state.messages:
            role = m.role.value if hasattr(m.role, "value") else str(m.role)
            if role == "user":
                lines.append(f"## User\n\n{m.content}\n")
            elif role == "assistant":
                if m.content:
                    lines.append(f"## Assistant\n\n{m.content}\n")
                for tc in (m.tool_calls or []):
                    lines.append(f"- 🔧 `{tc.name}` {tc.arguments}")
            elif role == "tool" and m.tool_result:
                body = (m.tool_result.content or "")[:1000]
                lines.append(f"> {body}\n")
        try:
            from pathlib import Path
            Path(path).write_text("\n".join(lines), encoding="utf-8")
            self._send_event("command_result", {"text": f"📄 Exported session to {path}"})
        except OSError as e:
            self._send_event("command_result", {"text": f"export failed: {e}"})

    def _forward_event(self, event: AgentEventData) -> None:
        """转发 agent 事件到 stdout"""
        event_map = {
            AgentEvent.THINKING: "thinking",
            AgentEvent.ASSISTANT_MESSAGE: "assistant_message",
            AgentEvent.TOOL_CALL: "tool_call",
            AgentEvent.TOOL_RESULT: "tool_result",
            AgentEvent.PERMISSION_REQUEST: "permission_request_event",
            AgentEvent.ERROR: "error",
            AgentEvent.DONE: "done",
            AgentEvent.COMPACTING: "compacting",
            AgentEvent.RETRYING: "retrying",
            AgentEvent.ROLLBACK: "rollback",
            AgentEvent.INTERRUPTED: "interrupted",
        }
        
        event_type = event_map.get(event.event, "unknown")
        self._send_event(event_type, event.data)

        # After each tool result, surface the current plan (if any) as a
        # structured `plan` event so the TUI can render a live todo panel
        # (Claude Code's TodoWrite panel). Cheap: just reads state metadata.
        if event.event == AgentEvent.TOOL_RESULT and self.state is not None:
            plan = self.state.metadata.get("plan")
            if plan:
                self._send_event("plan", {"steps": plan})
    
    async def run(self) -> None:
        """
        主循环：并发地从 stdin 读请求 + 执行 turn。

        关键：stdin 读取与 turn 执行解耦。一个 reader 任务持续把请求投入队列，
        dispatcher 逐条处理。user_input 的 turn 作为独立任务运行（见 handle_request），
        因此运行期间 reader 仍能读到 interrupt 请求并即时调用 agent_loop.interrupt()
        ——这是键盘 Esc 中断能生效的前提。
        """
        # 发送就绪信号
        self._send_event("ready", {
            "model": self.config.model,
            "tools": len(self.tool_registry.get_all_tools()),
            "auto_approve": self.config.auto_approve,
            "needs_setup": not self.config.api_key,
            "max_context_tokens": getattr(self.config, "max_context_tokens", 0),
        })

        loop = asyncio.get_event_loop()
        request_queue: asyncio.Queue = asyncio.Queue()

        async def _reader() -> None:
            while True:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    await request_queue.put(None)  # EOF
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    await request_queue.put(json.loads(line))
                except json.JSONDecodeError as e:
                    self._send_event("error", {"error": f"Invalid JSON: {e}"})

        reader_task = asyncio.ensure_future(_reader())
        try:
            while True:
                request = await request_queue.get()
                if request is None:
                    break  # EOF
                try:
                    await self.handle_request(request)
                except Exception as e:  # noqa: BLE001
                    self._send_event("error", {"error": str(e)})
        finally:
            reader_task.cancel()


async def main() -> None:
    """入口函数"""
    # 优先分层解析（读全局 config.json，让上次向导保存的配置生效），
    # 再让 init 首行 / env 覆盖。
    config = AgentConfig.resolve()

    if not config.api_key:
        # 尝试从 stdin 读取配置
        import sys
        first_line = sys.stdin.readline().strip()
        if first_line:
            try:
                init_config = json.loads(first_line)
                if init_config.get("type") == "init":
                    if init_config.get("api_key"):
                        config.api_key = init_config["api_key"]
                    config.api_base_url = init_config.get("api_base_url", config.api_base_url)
                    config.model = init_config.get("model", config.model)
                    config.auto_approve = init_config.get("auto_approve", config.auto_approve)
                    if init_config.get("protocol"):
                        config.protocol = init_config["protocol"]
                    if init_config.get("extra_headers"):
                        config.extra_headers = init_config["extra_headers"]
            except json.JSONDecodeError:
                pass

    # 无 key 也照常启动：ready 事件会带 needs_setup=true，前端可弹配置向导，
    # 通过 save_config 完成配置后再开始对话。
    protocol = AgentProtocol(config)
    await protocol.run()


if __name__ == "__main__":
    asyncio.run(main())
