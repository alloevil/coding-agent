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
        
        # 初始化工具
        register_file_tools()
        register_shell_tools()
        register_git_tools()
        
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
        )
        
        # 设置模型调用
        self.agent_loop.set_model_call_fn(self._call_model)
        
        # 设置权限确认
        self.agent_loop.set_permission_handler(self._confirm_permission)
        
        # 当前状态
        self.state: AgentState | None = None
        
        # 权限确认队列（等待 TUI 响应）
        self._permission_event = asyncio.Event()
        self._permission_result: bool = False
    
    async def _call_model(self, context: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """调用模型（流式）。委托给统一 ModelClient；文本增量转为 stream_text 事件。"""
        return await self.model_client.complete(
            context,
            tools,
            on_text_delta=lambda chunk: self._send_event("stream_text", {"text": chunk}),
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
            
            # 运行 agent
            async for event in self.agent_loop.run(self.state, content):
                self._forward_event(event)
            
            # 发送完成事件
            self._send_event("session_state", {
                "session_id": self.state.session_id,
                "turn_count": self.state.turn_count
            })
        
        elif req_type == "permission_response":
            self._permission_result = request.get("approved", False)
            self._permission_event.set()
        
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
        }
        
        event_type = event_map.get(event.event, "unknown")
        self._send_event(event_type, event.data)
    
    async def run(self) -> None:
        """主循环：从 stdin 读取请求"""
        # 发送就绪信号
        self._send_event("ready", {
            "model": self.config.model,
            "tools": len(self.tool_registry.get_all_tools()),
            "auto_approve": self.config.auto_approve
        })
        
        loop = asyncio.get_event_loop()
        
        while True:
            try:
                # 从 stdin 读取一行
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break  # EOF
                
                line = line.strip()
                if not line:
                    continue
                
                request = json.loads(line)
                await self.handle_request(request)
                
            except json.JSONDecodeError as e:
                self._send_event("error", {"error": f"Invalid JSON: {e}"})
            except Exception as e:
                self._send_event("error", {"error": str(e)})


async def main() -> None:
    """入口函数"""
    config = AgentConfig.from_env()
    
    if not config.api_key:
        # 尝试从 stdin 读取配置
        import sys
        first_line = sys.stdin.readline().strip()
        if first_line:
            try:
                init_config = json.loads(first_line)
                if init_config.get("type") == "init":
                    config.api_key = init_config.get("api_key", "")
                    config.api_base_url = init_config.get("api_base_url", config.api_base_url)
                    config.model = init_config.get("model", config.model)
                    config.auto_approve = init_config.get("auto_approve", config.auto_approve)
            except json.JSONDecodeError:
                pass
    
    if not config.api_key:
        print(json.dumps({"type": "error", "error": "No API key configured"}), flush=True)
        sys.exit(1)
    
    protocol = AgentProtocol(config)
    await protocol.run()


if __name__ == "__main__":
    asyncio.run(main())
