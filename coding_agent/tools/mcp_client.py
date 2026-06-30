"""
MCP 客户端 - 连接 MCP server，把其 tools 暴露为本地 Tool

参考 Model Context Protocol：与 server 通过 JSON-RPC 2.0 通信。支持两种 transport：
  - stdio：拉起子进程，通过 stdin/stdout 收发（本地 server）
  - http：通过 httpx 向 URL POST 请求（streamable-HTTP / SSE 远程 server）

实现最小可用子集：initialize 握手 / tools/list / tools/call。

设计：
  - 懒启动：首次需要时再连接
  - 失败降级：server 不可用时不影响其余工具
  - 权限：默认 EXECUTE（外部能力），可按需在策略层细化
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from ..tools.base import Tool, ToolPermission


class MCPError(Exception):
    pass


class _StdioTransport:
    """通过子进程 stdin/stdout 收发 JSON-RPC（本地 MCP server）。"""

    def __init__(self, command: list[str], env: dict[str, str] | None,
                 cwd: str | None, timeout: float):
        self.command = command
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None

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

    async def request(self, msg: dict[str, Any], is_notification: bool) -> dict[str, Any] | None:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise MCPError("stdio MCP server not started")
        data = (json.dumps(msg) + "\n").encode()
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()
        if is_notification:
            return None
        while True:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self.timeout)
            if not line:
                raise MCPError("stdio MCP server closed the connection")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == msg.get("id"):
                if "error" in resp:
                    raise MCPError(str(resp["error"]))
                return resp.get("result", {})
            # 其它（通知等）忽略

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


class _HttpTransport:
    """通过 httpx 向 URL POST JSON-RPC（streamable-HTTP / SSE 远程 server）。

    每个请求 POST 一次；响应可能是 application/json（单条）或
    text/event-stream（SSE，逐 data: 事件读取，取匹配 id 的那条）。
    """

    def __init__(self, url: str, headers: dict[str, str] | None, timeout: float):
        self.url = url
        self.headers = {"Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        **(headers or {})}
        self.timeout = timeout
        self._client: Any = None

    async def start(self) -> None:
        if self._client is not None:
            return
        import httpx
        self._client = httpx.AsyncClient(timeout=self.timeout)

    async def request(self, msg: dict[str, Any], is_notification: bool) -> dict[str, Any] | None:
        if self._client is None:
            raise MCPError("http MCP transport not started")
        resp = await self._client.post(self.url, headers=self.headers,
                                       content=json.dumps(msg))
        resp.raise_for_status()
        if is_notification:
            return None
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            return self._parse_sse(resp.text, msg.get("id"))
        data = resp.json()
        if "error" in data:
            raise MCPError(str(data["error"]))
        return data.get("result", {})

    @staticmethod
    def _parse_sse(text: str, want_id: Any) -> dict[str, Any]:
        """从 SSE 文本里取出匹配 id 的 JSON-RPC 响应。"""
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == want_id:
                if "error" in obj:
                    raise MCPError(str(obj["error"]))
                return obj.get("result", {})
        raise MCPError("no matching response in SSE stream")

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None


class MCPClient:
    """单个 MCP server 的 JSON-RPC 客户端（stdio 或 http transport）。"""

    def __init__(self, name: str, command: list[str] | None = None,
                 env: dict[str, str] | None = None, cwd: str | None = None,
                 timeout: float = 30.0, url: str | None = None,
                 transport: str | None = None, headers: dict[str, str] | None = None):
        self.name = name
        self.timeout = timeout
        self._req_id = 0
        self._lock = asyncio.Lock()
        self._initialized = False
        # 选择 transport：显式 transport 优先；否则 url→http，command→stdio。
        kind = transport or ("http" if url else "stdio")
        if kind in ("http", "sse"):
            if not url:
                raise MCPError(f"MCP server '{name}': http transport needs a url")
            self._transport: Any = _HttpTransport(url, headers, timeout)
        else:
            if not command:
                raise MCPError(f"MCP server '{name}': stdio transport needs a command")
            self._transport = _StdioTransport(command, env, cwd, timeout)

    async def start(self) -> None:
        if self._initialized:
            return
        await self._transport.start()
        await self._initialize()

    async def _send(self, method: str, params: dict[str, Any] | None = None,
                    is_notification: bool = False) -> dict[str, Any] | None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not is_notification:
            self._req_id += 1
            msg["id"] = self._req_id
        try:
            return await self._transport.request(msg, is_notification)
        except MCPError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MCPError(f"{self.name}: {e}") from e

    async def _initialize(self) -> None:
        await self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "coding-agent", "version": "0.2"},
        })
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
        await self._transport.close()
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
    连接配置里的 MCP servers，注册它们的工具。支持 stdio 与 http/sse transport：

      stdio: {"fs": {"command": ["npx","-y","@mcp/server-fs","/path"]}}
      http:  {"gh": {"url": "https://mcp.example.com/", "headers": {"Authorization": "Bearer ..."}}}
             或显式 {"x": {"transport": "sse", "url": "..."}}

    返回已连接的 client 列表（供后续 close）。单个 server 失败不影响其余。
    """
    clients: list[MCPClient] = []
    for name, cfg in (servers or {}).items():
        command = cfg.get("command")
        if isinstance(command, str):
            command = command.split()
        url = cfg.get("url")
        transport = cfg.get("transport")
        if not command and not url:
            continue
        try:
            client = MCPClient(
                name=name, command=command, env=cfg.get("env"),
                cwd=cfg.get("cwd"), url=url, transport=transport,
                headers=cfg.get("headers"),
            )
            tools = await client.list_tools()
        except Exception:
            # server 不可用：跳过，不影响其它工具
            try:
                await client.close()  # type: ignore[has-type]
            except Exception:
                pass
            continue
        for spec in tools:
            registry.register(MCPTool(client, spec))
        clients.append(client)
    return clients
