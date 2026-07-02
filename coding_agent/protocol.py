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
        )
        
        # 设置模型调用
        self.agent_loop.set_model_call_fn(self._call_model)
        self.agent_loop.set_token_usage_fn(
            lambda: self.model_client.total_prompt_tokens
            + self.model_client.total_completion_tokens
        )
        
        # 设置权限确认
        self.agent_loop.set_permission_handler(self._confirm_permission)
        
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
        推理增量转为 stream_reasoning 事件（供 TUI 显示思考过程）。"""
        return await self.model_client.complete(
            context,
            tools,
            on_text_delta=lambda chunk: self._send_event("stream_text", {"text": chunk}),
            on_reasoning_delta=lambda chunk: self._send_event("stream_reasoning", {"text": chunk}),
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
                self._send_event("session_state", {
                    "session_id": self.state.session_id,
                    "turn_count": self.state.turn_count,
                })
            self._turn_task = None

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
