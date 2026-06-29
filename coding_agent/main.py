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
import urllib.request
import json
from typing import Any

from .core import AgentLoop, AgentConfig, AgentState, AgentEvent, AgentEventData
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
        self.config = config or AgentConfig.from_env()
        
        # 初始化工具
        self.tool_registry = get_registry()
        register_file_tools()
        register_shell_tools()
        register_git_tools()
        register_browser_tools()
        register_lsp_tools()
        
        # 初始化存储
        self.session_store = SessionStore(self.config.session_db_path)
        
        # 初始化 Agent Loop
        self.agent_loop = AgentLoop(
            config=self.config,
            tool_registry=self.tool_registry,
            session_store=self.session_store
        )
        
        # 设置模型调用函数
        self.agent_loop.set_model_call_fn(self._call_model)
        
        # 设置权限确认回调
        self.agent_loop.set_permission_handler(self._confirm_permission)
        
        # 当前状态
        self.state: AgentState | None = None
    
    async def _call_model(self, context: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """
        调用模型（支持流式输出）
        
        支持 OpenAI 兼容 API（包括小米 mify）
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}"
        }
        
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": context,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": True,  # 启用流式输出
        }
        
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        
        # 流式调用
        def _stream_call():
            req = urllib.request.Request(
                f"{self.config.api_base_url}/chat/completions",
                data=json.dumps(payload).encode(),
                headers=headers
            )
            return urllib.request.urlopen(req, timeout=120)
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _stream_call)
        
        # 解析 SSE 流
        content_parts = []
        tool_calls_data = []
        current_tool_call = None
        
        for line in response:
            line = line.decode("utf-8", errors="replace").strip()
            if not line or line == "data: [DONE]":
                continue
            if not line.startswith("data: "):
                continue
            
            try:
                data = json.loads(line[6:])
                choices = data.get("choices", [])
                if not choices:
                    continue
                
                delta = choices[0].get("delta", {})
                
                # 内容
                if delta.get("content"):
                    chunk = delta["content"]
                    content_parts.append(chunk)
                    print(chunk, end="", flush=True)
                
                # 工具调用
                if delta.get("tool_calls"):
                    for tc_delta in delta["tool_calls"]:
                        tc_index = tc_delta.get("index", 0)
                        
                        # 确保有足够的工具调用槽位
                        while len(tool_calls_data) <= tc_index:
                            tool_calls_data.append({
                                "id": "",
                                "function": {"name": "", "arguments": ""}
                            })
                        
                        tc = tool_calls_data[tc_index]
                        
                        if tc_delta.get("id"):
                            tc["id"] = tc_delta["id"]
                        if tc_delta.get("function", {}).get("name"):
                            tc["function"]["name"] += tc_delta["function"]["name"]
                        if tc_delta.get("function", {}).get("arguments"):
                            tc["function"]["arguments"] += tc_delta["function"]["arguments"]
                            
            except json.JSONDecodeError:
                continue
        
        return {
            "content": "".join(content_parts),
            "tool_calls": tool_calls_data if tool_calls_data else []
        }
    
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
        
        print("🤖 Coding Agent started!")
        print(f"   Session: {self.state.session_id}")
        print(f"   Model: {self.config.model}")
        print(f"   Auto-approve: {'ON' if self.config.auto_approve else 'OFF'}")
        print(f"   Tools: {len(self.tool_registry.get_all_tools())}")
        print()
        print("Commands: quit, new, sessions, help")
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
            
            if user_input.lower() == "quit":
                print("Goodbye!")
                break
            
            if user_input.lower() == "new":
                self.state = AgentState(
                    session_id=self.session_store.create_session()
                )
                print("✨ Started new session")
                continue
            
            if user_input.lower() == "sessions":
                sessions = self.session_store.list_sessions()
                print("\n📋 Recent sessions:")
                for s in sessions[:5]:
                    print(f"   {s['id'][:8]}... ({s['updated_at']})")
                continue
            
            if user_input.lower() == "help":
                print("""
Commands:
  quit      - Exit the agent
  new       - Start a new session
  sessions  - List recent sessions
  help      - Show this help

Permissions:
  y/yes     - Allow this tool call
  n/no      - Deny this tool call
  a/all     - Allow all tool calls this session
""")
                continue
            
            # 运行 Agent
            print()
            
            async for event in self.agent_loop.run(self.state, user_input):
                await self._handle_event(event)
    
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
        
        elif event.event == AgentEvent.DONE:
            turns = event.data["turns"]
            print(f"\n{'=' * 50}")
            print(f"✨ Done in {turns} turns")


async def main() -> None:
    """主函数"""
    config = AgentConfig.from_env()
    
    # 检查 API key
    if not config.api_key:
        print("Error: No API key configured")
        print("Set OPENAI_API_KEY or LLM_API_KEY environment variable")
        sys.exit(1)
    
    agent = CodingAgent(config)
    await agent.start()


def cli() -> None:
    """Synchronous entry point for the `coding-agent` console script."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGoodbye!")


if __name__ == "__main__":
    cli()
