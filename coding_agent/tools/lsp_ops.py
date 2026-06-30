"""
LSP 语义理解工具

通过 LSP（Language Server Protocol）提供代码语义分析：
- goto_definition: 跳转到定义
- find_references: 查找引用
- hover: 悬停查看类型/文档
- diagnostics: 获取诊断信息
- symbols: 列出文件符号

支持的语言服务器：
- Python: python-lsp-server (pylsp)
- TypeScript/JavaScript: typescript-language-server
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .base import Tool, ToolPermission, ToolExecutionError


# ─── LSP JSON-RPC Client ────────────────────────────────────────────

class LSPClient:
    """
    LSP JSON-RPC 客户端

    通过 stdin/stdout 与 language server 进程通信。
    管理请求/响应的 id 匹配。
    """

    def __init__(self, process: asyncio.subprocess.Process, name: str):
        self._process = process
        self._name = name
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._read_task: asyncio.Task | None = None
        self._initialized = False
        # 已打开文档：uri -> (version, content)。用于决定 didOpen vs didChange，
        # 让编辑后的内容真正同步给 server（否则诊断会过时）。
        self._open_docs: dict[str, tuple[int, str]] = {}

    @property
    def name(self) -> str:
        return self._name

    async def start(self) -> None:
        """启动读取循环"""
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """从 stdout 读取 JSON-RPC 消息"""
        reader = self._process.stdout
        while True:
            # 读取 header
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if not line:
                    return  # 进程已退出
                line_str = line.decode("utf-8").strip()
                if line_str == "":
                    break
                if ":" in line_str:
                    key, value = line_str.split(":", 1)
                    headers[key.strip()] = value.strip()

            content_length = int(headers.get("Content-Length", 0))
            if content_length == 0:
                continue

            # 读取 body
            body = await reader.readexactly(content_length)
            message = json.loads(body.decode("utf-8"))

            # 处理响应
            msg_id = message.get("id")
            if msg_id is not None and msg_id in self._pending:
                future = self._pending.pop(msg_id)
                if "error" in message:
                    future.set_exception(
                        LSPError(message["error"].get("message", "Unknown LSP error"))
                    )
                else:
                    future.set_result(message.get("result"))

    async def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """发送请求并等待响应"""
        self._request_id += 1
        req_id = self._request_id

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        message = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            message["params"] = params

        await self._send(message)

        try:
            return await asyncio.wait_for(future, timeout=30)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise LSPError(f"Request {method} timed out after 30s")

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """发送通知（不需要等待响应）"""
        message = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        asyncio.create_task(self._send(message))

    async def _send(self, message: dict[str, Any]) -> None:
        """发送 JSON-RPC 消息"""
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        self._process.stdin.write(header + body)
        await self._process.stdin.drain()

    async def initialize(self, root_uri: str) -> dict[str, Any]:
        """执行 LSP initialize 握手"""
        result = await self.send_request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "synchronization": {
                        "dynamicRegistration": False,
                        "didSave": False,
                    },
                    "definition": {"dynamicRegistration": False},
                    "implementation": {"dynamicRegistration": False},
                    "references": {"dynamicRegistration": False},
                    "hover": {
                        "dynamicRegistration": False,
                        "contentFormat": ["plaintext", "markdown"],
                    },
                    "publishDiagnostics": {},
                    "documentSymbol": {
                        "dynamicRegistration": False,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                },
                "workspace": {
                    "symbol": {
                        "dynamicRegistration": False,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                },
            },
        })
        self.send_notification("initialized", {})
        self._initialized = True
        return result

    @property
    def is_ready(self) -> bool:
        return self._initialized and self._process.returncode is None

    async def shutdown(self) -> None:
        """优雅关闭"""
        try:
            await self.send_request("shutdown")
            self.send_notification("exit")
        except Exception:
            pass
        finally:
            if self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._process.kill()
            if self._read_task and not self._read_task.done():
                self._read_task.cancel()


class LSPError(Exception):
    """LSP 相关错误"""
    pass


# ─── Language Server Manager ─────────────────────────────────────────

# 语言 → (命令列表, 文件扩展名) 映射
LANGUAGE_SERVERS: dict[str, tuple[list[str], list[str]]] = {
    "python": (["pylsp"], [".py"]),
    "typescript": (["typescript-language-server", "--stdio"], [".ts", ".tsx"]),
    "javascript": (["typescript-language-server", "--stdio"], [".js", ".jsx"]),
}

# 扩展名 → 语言反查
_EXT_TO_LANG: dict[str, str] = {}
for _lang, (_, _exts) in LANGUAGE_SERVERS.items():
    for _ext in _exts:
        _EXT_TO_LANG[_ext] = _lang


def detect_language(file_path: str) -> str | None:
    """根据文件扩展名检测语言"""
    ext = Path(file_path).suffix.lower()
    return _EXT_TO_LANG.get(ext)


def uri_from_path(file_path: str) -> str:
    """文件路径转 LSP URI"""
    abs_path = Path(file_path).resolve()
    return f"file://{abs_path}"


class LSPServerManager:
    """
    管理多个 Language Server 实例

    按语言缓存 server，支持复用和关闭。
    """

    def __init__(self) -> None:
        self._clients: dict[str, LSPClient] = {}  # lang → client
        self._root_uri: str = ""

    def set_root(self, root_path: str) -> None:
        """设置工作区根路径"""
        self._root_uri = uri_from_path(root_path)

    async def get_client(self, language: str) -> LSPClient:
        """获取或创建指定语言的 LSP 客户端"""
        if language in self._clients and self._clients[language].is_ready:
            return self._clients[language]

        # 关闭旧的（如果有）
        if language in self._clients:
            await self._clients[language].shutdown()

        # 检查命令是否可用
        if language not in LANGUAGE_SERVERS:
            raise LSPError(f"Unsupported language: {language}. Supported: {list(LANGUAGE_SERVERS.keys())}")

        cmd, _ = LANGUAGE_SERVERS[language]
        if not shutil.which(cmd[0]):
            raise LSPError(
                f"Language server '{cmd[0]}' not found. "
                f"Install it with: {'pip install python-lsp-server' if cmd[0] == 'pylsp' else 'npm install -g typescript-language-server typescript'}"
            )

        # 启动进程
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise LSPError(f"Failed to start language server: {cmd[0]}")

        client = LSPClient(process, language)
        await client.start()
        await client.initialize(self._root_uri or uri_from_path(os.getcwd()))

        self._clients[language] = client
        return client

    async def close_all(self) -> None:
        """关闭所有 language server"""
        for client in self._clients.values():
            await client.shutdown()
        self._clients.clear()


# 全局单例
_server_manager: LSPServerManager | None = None


def get_server_manager() -> LSPServerManager:
    """获取全局 LSP Server Manager"""
    global _server_manager
    if _server_manager is None:
        _server_manager = LSPServerManager()
    return _server_manager


# ─── Symbol Kind 映射 ────────────────────────────────────────────────

SYMBOL_KIND_NAMES: dict[int, str] = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package",
    5: "Class", 6: "Method", 7: "Property", 8: "Field",
    9: "Constructor", 10: "Enum", 11: "Interface", 12: "Function",
    13: "Variable", 14: "Constant", 15: "String", 16: "Number",
    17: "Boolean", 18: "Array", 19: "Object", 20: "Key",
    21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
    25: "Operator", 26: "TypeParameter",
}


# ─── 辅助函数 ────────────────────────────────────────────────────────

async def _open_text_document(client: LSPClient, file_path: str) -> str:
    """
    同步文件到 LSP server，返回 file_uri。

    首次见到该 uri → didOpen；之后若内容变化 → didChange（version 递增）。
    这样编辑过的文件能把最新内容同步给 server，诊断不再过时。
    """
    abs_path = str(Path(file_path).resolve())
    file_uri = f"file://{abs_path}"

    content = Path(abs_path).read_text(encoding="utf-8")

    # 检测语言
    ext = Path(abs_path).suffix.lower()
    lang_id_map = {
        ".py": "python",
        ".ts": "typescript", ".tsx": "typescriptreact",
        ".js": "javascript", ".jsx": "javascriptreact",
    }
    language_id = lang_id_map.get(ext, "")

    prev = client._open_docs.get(file_uri)
    if prev is None:
        client.send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": file_uri,
                "languageId": language_id,
                "version": 1,
                "text": content,
            }
        })
        client._open_docs[file_uri] = (1, content)
        await asyncio.sleep(0.3)
    elif prev[1] != content:
        # 内容变了 → 发 didChange（全量同步），version 递增
        version = prev[0] + 1
        client.send_notification("textDocument/didChange", {
            "textDocument": {"uri": file_uri, "version": version},
            "contentChanges": [{"text": content}],
        })
        client._open_docs[file_uri] = (version, content)
        await asyncio.sleep(0.3)
    return file_uri


def _format_location(loc: dict[str, Any]) -> str:
    """格式化 LSP Location"""
    uri = loc.get("uri", "")
    range_ = loc.get("range", {})
    start = range_.get("start", {})
    file_path = uri.replace("file://", "")
    return f"{file_path}:{start.get('line', 0) + 1}:{start.get('character', 0)}"


def _format_position(pos: dict[str, Any]) -> tuple[int, int]:
    """LSP position → (line, character)"""
    return pos.get("line", 0) + 1, pos.get("character", 0)


# ─── Tools ───────────────────────────────────────────────────────────

class LSPGotoDefinitionTool(Tool):
    """跳转到定义"""

    @property
    def name(self) -> str:
        return "lsp_goto_definition"

    @property
    def description(self) -> str:
        return "Go to the definition of a symbol at the given position. Returns the file path, line, and column of the definition."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path containing the symbol",
                },
                "line": {
                    "type": "integer",
                    "description": "Line number (1-indexed)",
                },
                "column": {
                    "type": "integer",
                    "description": "Column number (0-indexed)",
                },
            },
            "required": ["path", "line", "column"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")
        line = kwargs.get("line")
        column = kwargs.get("column")

        if not path:
            raise ToolExecutionError(self.name, "path is required")
        if line is None or column is None:
            raise ToolExecutionError(self.name, "line and column are required")

        try:
            lang = detect_language(path)
            if not lang:
                return f"Error: Unsupported file type for '{path}'"

            manager = get_server_manager()
            client = await manager.get_client(lang)
            file_uri = await _open_text_document(client, path)

            result = await client.send_request("textDocument/definition", {
                "textDocument": {"uri": file_uri},
                "position": {"line": line - 1, "character": column},
            })

            if not result:
                return "No definition found"

            # result 可能是单个 Location 或 Location[]
            locations = result if isinstance(result, list) else [result]

            output_lines = []
            for loc in locations:
                uri = loc.get("uri", "")
                range_ = loc.get("range", {})
                start = range_.get("start", {})
                file_path = uri.replace("file://", "")
                start_line = start.get("line", 0) + 1
                start_char = start.get("character", 0)

                # 读取定义处的代码行
                code_line = ""
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                        if start_line <= len(lines):
                            code_line = lines[start_line - 1].rstrip()
                except Exception:
                    pass

                output_lines.append(f"{file_path}:{start_line}:{start_char}")
                if code_line:
                    output_lines.append(f"  → {code_line}")

            return "\n".join(output_lines)

        except LSPError as e:
            return f"LSP Error: {e}"
        except Exception as e:
            return f"Error: {e}"


class LSPFindReferencesTool(Tool):
    """查找引用"""

    @property
    def name(self) -> str:
        return "lsp_find_references"

    @property
    def description(self) -> str:
        return "Find all references to a symbol at the given position. Returns all locations where the symbol is used."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path containing the symbol",
                },
                "line": {
                    "type": "integer",
                    "description": "Line number (1-indexed)",
                },
                "column": {
                    "type": "integer",
                    "description": "Column number (0-indexed)",
                },
                "include_declaration": {
                    "type": "boolean",
                    "description": "Include the declaration in results (default: true)",
                },
            },
            "required": ["path", "line", "column"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")
        line = kwargs.get("line")
        column = kwargs.get("column")
        include_declaration = kwargs.get("include_declaration", True)

        if not path:
            raise ToolExecutionError(self.name, "path is required")
        if line is None or column is None:
            raise ToolExecutionError(self.name, "line and column are required")

        try:
            lang = detect_language(path)
            if not lang:
                return f"Error: Unsupported file type for '{path}'"

            manager = get_server_manager()
            client = await manager.get_client(lang)
            file_uri = await _open_text_document(client, path)

            result = await client.send_request("textDocument/references", {
                "textDocument": {"uri": file_uri},
                "position": {"line": line - 1, "character": column},
                "context": {"includeDeclaration": include_declaration},
            })

            if not result:
                return "No references found"

            output_lines = [f"Found {len(result)} reference(s):"]
            for loc in result:
                output_lines.append(f"  {_format_location(loc)}")

            return "\n".join(output_lines)

        except LSPError as e:
            return f"LSP Error: {e}"
        except Exception as e:
            return f"Error: {e}"


class LSPHoverTool(Tool):
    """悬停查看类型/文档"""

    @property
    def name(self) -> str:
        return "lsp_hover"

    @property
    def description(self) -> str:
        return "Get hover information (type signature, documentation) for a symbol at the given position."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path containing the symbol",
                },
                "line": {
                    "type": "integer",
                    "description": "Line number (1-indexed)",
                },
                "column": {
                    "type": "integer",
                    "description": "Column number (0-indexed)",
                },
            },
            "required": ["path", "line", "column"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")
        line = kwargs.get("line")
        column = kwargs.get("column")

        if not path:
            raise ToolExecutionError(self.name, "path is required")
        if line is None or column is None:
            raise ToolExecutionError(self.name, "line and column are required")

        try:
            lang = detect_language(path)
            if not lang:
                return f"Error: Unsupported file type for '{path}'"

            manager = get_server_manager()
            client = await manager.get_client(lang)
            file_uri = await _open_text_document(client, path)

            result = await client.send_request("textDocument/hover", {
                "textDocument": {"uri": file_uri},
                "position": {"line": line - 1, "character": column},
            })

            if not result:
                return "No hover information available"

            contents = result.get("contents", {})
            if isinstance(contents, str):
                return contents
            elif isinstance(contents, dict):
                return contents.get("value", str(contents))
            elif isinstance(contents, list):
                parts = []
                for item in contents:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        parts.append(item.get("value", str(item)))
                return "\n".join(parts)

            return str(contents)

        except LSPError as e:
            return f"LSP Error: {e}"
        except Exception as e:
            return f"Error: {e}"


class LSPDiagnosticsTool(Tool):
    """获取文件诊断"""

    @property
    def name(self) -> str:
        return "lsp_diagnostics"

    @property
    def description(self) -> str:
        return "Get diagnostics (errors, warnings, hints) for a file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to check diagnostics for",
                },
            },
            "required": ["path"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")

        if not path:
            raise ToolExecutionError(self.name, "path is required")

        try:
            lang = detect_language(path)
            if not lang:
                return f"Error: Unsupported file type for '{path}'"

            manager = get_server_manager()
            client = await manager.get_client(lang)
            file_uri = await _open_text_document(client, path)

            # 一些 LSP server 在 didOpen 后会异步推送 diagnostics
            # 我们用 textDocument/diagnostic 如果支持，否则检查已缓存的结果
            # 作为替代方案，发送 documentSymbol 触发分析，然后等一小段时间
            try:
                result = await client.send_request("textDocument/diagnostic", {
                    "textDocument": {"uri": file_uri},
                })
                diagnostics = result.get("items", []) if result else []
            except LSPError:
                # server 不支持 pull diagnostics，尝试其他方式
                diagnostics = []

            if not diagnostics:
                return f"No diagnostics found for '{path}' (or server does not support pull diagnostics)"

            severity_map = {1: "ERROR", 2: "WARNING", 3: "INFO", 4: "HINT"}
            output_lines = [f"Found {len(diagnostics)} diagnostic(s):"]

            for diag in diagnostics:
                severity = severity_map.get(diag.get("severity", 0), "UNKNOWN")
                range_ = diag.get("range", {})
                start = range_.get("start", {})
                line_num = start.get("line", 0) + 1
                col = start.get("character", 0)
                message = diag.get("message", "")
                source = diag.get("source", "")
                prefix = f"  [{severity}] {path}:{line_num}:{col}"
                if source:
                    prefix += f" ({source})"
                output_lines.append(f"{prefix}: {message}")

            return "\n".join(output_lines)

        except LSPError as e:
            return f"LSP Error: {e}"
        except Exception as e:
            return f"Error: {e}"


class LSPSymbolsTool(Tool):
    """列出文件中的所有符号"""

    @property
    def name(self) -> str:
        return "lsp_symbols"

    @property
    def description(self) -> str:
        return "List all symbols (classes, functions, variables, etc.) in a file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to list symbols for",
                },
            },
            "required": ["path"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")

        if not path:
            raise ToolExecutionError(self.name, "path is required")

        try:
            lang = detect_language(path)
            if not lang:
                return f"Error: Unsupported file type for '{path}'"

            manager = get_server_manager()
            client = await manager.get_client(lang)
            file_uri = await _open_text_document(client, path)

            result = await client.send_request("textDocument/documentSymbol", {
                "textDocument": {"uri": file_uri},
            })

            if not result:
                return f"No symbols found in '{path}'"

            output_lines = [f"Symbols in '{path}':"]
            self._format_symbols(result, output_lines, indent=1)

            return "\n".join(output_lines)

        except LSPError as e:
            return f"LSP Error: {e}"
        except Exception as e:
            return f"Error: {e}"

    def _format_symbols(self, symbols: list[dict], output: list[str], indent: int = 1) -> None:
        """递归格式化符号树"""
        for sym in symbols:
            kind = SYMBOL_KIND_NAMES.get(sym.get("kind", 0), "Unknown")
            name = sym.get("name", "")
            range_ = sym.get("range", {})
            start = range_.get("start", {})
            line_num = start.get("line", 0) + 1
            col = start.get("character", 0)

            prefix = "  " * indent
            output.append(f"{prefix}[{kind}] {name} (line {line_num}, col {col})")

            # 递归处理子符号
            children = sym.get("children", [])
            if children:
                self._format_symbols(children, output, indent + 1)


# ─── Registration ────────────────────────────────────────────────────

class LSPImplementationTool(Tool):
    """跳转到接口/抽象方法的实现"""

    @property
    def name(self) -> str:
        return "lsp_implementation"

    @property
    def description(self) -> str:
        return ("Find implementations of the symbol at the given position "
                "(e.g. implementations of an interface or abstract method). "
                "Returns file:line:col locations.")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path containing the symbol"},
                "line": {"type": "integer", "description": "Line number (1-indexed)"},
                "column": {"type": "integer", "description": "Column number (0-indexed)"},
            },
            "required": ["path", "line", "column"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")
        line = kwargs.get("line")
        column = kwargs.get("column")
        if not path:
            raise ToolExecutionError(self.name, "path is required")
        if line is None or column is None:
            raise ToolExecutionError(self.name, "line and column are required")
        try:
            lang = detect_language(path)
            if not lang:
                return f"Error: Unsupported file type for '{path}'"
            manager = get_server_manager()
            client = await manager.get_client(lang)
            file_uri = await _open_text_document(client, path)
            result = await client.send_request("textDocument/implementation", {
                "textDocument": {"uri": file_uri},
                "position": {"line": line - 1, "character": column},
            })
            if not result:
                return "No implementations found"
            locations = result if isinstance(result, list) else [result]
            return "\n".join(_format_location(loc) for loc in locations)
        except LSPError as e:
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001
            return f"Error running lsp_implementation: {e}"


class LSPWorkspaceSymbolsTool(Tool):
    """按名在整个工作区搜索符号"""

    @property
    def name(self) -> str:
        return "lsp_workspace_symbols"

    @property
    def description(self) -> str:
        return ("Search for symbols (functions, classes, variables) by name across "
                "the whole workspace. Returns matching symbols with their locations.")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Symbol name or substring to search for"},
                "language": {"type": "string",
                             "description": "Optional language hint (python/typescript/...) "
                                            "to pick the language server"},
            },
            "required": ["query"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query")
        if not query:
            raise ToolExecutionError(self.name, "query is required")
        lang = kwargs.get("language") or "python"
        try:
            manager = get_server_manager()
            client = await manager.get_client(lang)
            result = await client.send_request("workspace/symbol", {"query": query})
            if not result:
                return f"No symbols matching '{query}'"
            lines = []
            for sym in result[:50]:
                name = sym.get("name", "?")
                loc = sym.get("location", {})
                lines.append(f"{name}  —  {_format_location(loc)}")
            return "\n".join(lines)
        except LSPError as e:
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001
            return f"Error running lsp_workspace_symbols: {e}"


def register_lsp_tools(registry: Any = None) -> None:
    """注册所有 LSP 工具"""
    from .registry import get_registry

    reg = registry or get_registry()
    reg.register(LSPGotoDefinitionTool())
    reg.register(LSPFindReferencesTool())
    reg.register(LSPHoverTool())
    reg.register(LSPDiagnosticsTool())
    reg.register(LSPSymbolsTool())
    reg.register(LSPImplementationTool())
    reg.register(LSPWorkspaceSymbolsTool())
