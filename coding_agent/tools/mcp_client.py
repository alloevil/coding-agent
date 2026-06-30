"""
MCP 客户端 - 通过 stdio 连接 MCP server，把其 tools 暴露为本地 Tool

参考 Model Context Protocol：与 server 通过 stdin/stdout 上的
JSON-RPC 2.0 通信。这里实现一个最小可用子集：
  - initialize 握手
  - tools/list 列出工具
  - tools/call 调用工具

无新依赖（仅 asyncio.subprocess + json）。每个远程工具被包装成
MCPTool 注册进 registry，agent 即可像本地工具一样调用。

设计：
  - 懒启动：首次需要时再拉起 server 进程
  - 失败降级：server 不可用时不影响其余工具
  - 权限：默认 EXECUTE（外部进程），可按需在策略层细化
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from ..tools.base import Tool, ToolPermission


class MCPError(Exception):
    pass


class MCPClient:
    """单个 MCP server 的 stdio JSON-RPC 客户端。"""

    def __init__(self, name: str, command: list[str], env: dict[str, str] | None = None,
                 cwd: str | None = None, timeout: float = 30.0):
        self.name = name
        self.command = command
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        self._lock = asyncio.Lock()
        self._initialized = False

    async def start(self) -> None:
        if self._proc is not None:
            return
        import os
        full_env = {**os.environ, **(self.env or {})}
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
            cwd=self.cwd,
        )
        await self._initialize()

    async def _send(self, method: str, params: dict[str, Any] | None = None,
                    is_notification: bool = False) -> dict[str, Any] | None:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise MCPError(f"MCP server '{self.name}' not started")

        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not is_notification:
            self._req_id += 1
            msg["id"] = self._req_id

        data = (json.dumps(msg) + "\n").encode()
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

        if is_notification:
            return None

        # 读取直到拿到匹配 id 的响应（跳过通知/无关消息）
        while True:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self.timeout)
            if not line:
                raise MCPError(f"MCP server '{self.name}' closed the connection")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == msg.get("id"):
                if "error" in resp:
                    raise MCPError(f"{self.name}: {resp['error']}")
                return resp.get("result", {})
            # 其它（通知等）忽略

    async def _initialize(self) -> None:
        await self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "coding-agent", "version": "0.2"},
        })
        # 通知 server 初始化完成
        await self._send("notifications/initialized", {}, is_notification=True)
        self._initialized = True

    async def list_tools(self) -> list[dict[str, Any]]:
        async with self._lock:
            await self.start()
            result = await self._send("tools/list")
        return (result or {}).get("tools", [])

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        async with self._lock:
            await self.start()
            result = await self._send("tools/call", {
                "name": tool_name,
                "arguments": arguments,
            })
        return _render_tool_result(result or {})

    async def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            self._proc = None
            self._initialized = False


def _render_tool_result(result: dict[str, Any]) -> str:
    """把 MCP tools/call 的结果渲染成文本。"""
    content = result.get("content", [])
    parts = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        else:
            parts.append(str(item))
    text = "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)
    if result.get("isError"):
        return f"Error: {text}"
    return text


class MCPTool(Tool):
    """把一个 MCP 远程工具包装成本地 Tool。"""

    def __init__(self, client: MCPClient, spec: dict[str, Any]):
        self._client = client
        self._spec = spec
        # 加 server 前缀避免与本地工具/其它 server 冲突
        self._name = f"mcp__{client.name}__{spec['name']}"
        self._remote_name = spec["name"]

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._spec.get("description", f"MCP tool {self._remote_name}")

    @property
    def parameters(self) -> dict[str, Any]:
        return self._spec.get("inputSchema") or {"type": "object", "properties": {}}

    @property
    def permission(self) -> ToolPermission:
        # 外部进程，保守地按 EXECUTE 处理（可在权限策略层细化）
        return ToolPermission.EXECUTE

    async def execute(self, **kwargs: Any) -> str:
        return await self._client.call_tool(self._remote_name, kwargs)


async def register_mcp_servers(servers: dict[str, Any], registry: Any) -> list[MCPClient]:
    """
    连接配置里的 MCP servers，注册它们的工具。

    servers 形如：
      {"fs": {"command": ["npx","-y","@modelcontextprotocol/server-filesystem","/path"]}}

    返回已连接的 client 列表（供后续 close）。单个 server 失败不影响其余。
    """
    clients: list[MCPClient] = []
    for name, cfg in (servers or {}).items():
        command = cfg.get("command")
        if isinstance(command, str):
            command = command.split()
        if not command:
            continue
        client = MCPClient(name=name, command=command,
                           env=cfg.get("env"), cwd=cfg.get("cwd"))
        try:
            tools = await client.list_tools()
        except Exception:
            # server 不可用：跳过，不影响其它工具
            await client.close()
            continue
        for spec in tools:
            registry.register(MCPTool(client, spec))
        clients.append(client)
    return clients
