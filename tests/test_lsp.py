"""
测试 LSP 工具集

测试内容：
- LSPClient 协议实现（mock）
- LSPServerManager 生命周期
- 各工具的参数校验和基本逻辑
- 语言检测
- URI 转换
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from coding_agent.tools.lsp_ops import (
    LSPClient,
    LSPServerManager,
    LSPError,
    detect_language,
    uri_from_path,
    LANGUAGE_SERVERS,
    _EXT_TO_LANG,
    _format_location,
    SYMBOL_KIND_NAMES,
    LSPGotoDefinitionTool,
    LSPFindReferencesTool,
    LSPHoverTool,
    LSPDiagnosticsTool,
    LSPSymbolsTool,
    register_lsp_tools,
    get_server_manager,
)


# ─── 语言检测测试 ────────────────────────────────────────────────────

class TestDetectLanguage:
    """测试文件语言检测"""

    def test_python_file(self):
        assert detect_language("main.py") == "python"
        assert detect_language("/src/utils/helpers.py") == "python"

    def test_typescript_file(self):
        assert detect_language("app.ts") == "typescript"
        assert detect_language("component.tsx") == "typescript"

    def test_javascript_file(self):
        assert detect_language("index.js") == "javascript"
        assert detect_language("App.jsx") == "javascript"

    def test_unsupported_file(self):
        assert detect_language("README.md") is None
        assert detect_language("style.css") is None
        assert detect_language("data.json") is None

    def test_case_insensitive(self):
        assert detect_language("Main.PY") == "python"
        assert detect_language("App.TS") == "typescript"

    def test_ext_to_lang_mapping(self):
        """验证所有 LANGUAGE_SERVERS 的扩展名都有对应映射"""
        for lang, (_, exts) in LANGUAGE_SERVERS.items():
            for ext in exts:
                assert ext in _EXT_TO_LANG
                assert _EXT_TO_LANG[ext] == lang


# ─── URI 转换测试 ────────────────────────────────────────────────────

class TestUriConversion:
    """测试文件路径与 URI 转换"""

    def test_relative_path(self):
        uri = uri_from_path("main.py")
        assert uri.startswith("file://")
        assert uri.endswith("/main.py")

    def test_absolute_path(self):
        uri = uri_from_path("/tmp/test.py")
        assert uri == "file:///tmp/test.py"


# ─── 格式化工具测试 ─────────────────────────────────────────────────

class TestFormatHelpers:
    """测试辅助格式化函数"""

    def test_format_location(self):
        loc = {
            "uri": "file:///tmp/test.py",
            "range": {
                "start": {"line": 9, "character": 4},
                "end": {"line": 9, "character": 10},
            },
        }
        result = _format_location(loc)
        assert "/tmp/test.py:10:4" in result  # line 0-indexed → 1-indexed

    def test_symbol_kind_names(self):
        """确保常用的 symbol kind 都有名称映射"""
        assert SYMBOL_KIND_NAMES[5] == "Class"
        assert SYMBOL_KIND_NAMES[12] == "Function"
        assert SYMBOL_KIND_NAMES[13] == "Variable"
        assert SYMBOL_KIND_NAMES[6] == "Method"


# ─── LSPClient 测试 ─────────────────────────────────────────────────

class TestLSPClient:
    """测试 LSP 客户端"""

    def _make_mock_process(self):
        """创建 mock 的 asyncio subprocess"""
        proc = AsyncMock()
        proc.stdin = AsyncMock()
        proc.stdout = AsyncMock()
        proc.returncode = None
        return proc

    def test_client_name(self):
        proc = self._make_mock_process()
        client = LSPClient(proc, "python")
        assert client.name == "python"

    def test_client_not_ready_before_init(self):
        proc = self._make_mock_process()
        client = LSPClient(proc, "python")
        assert not client.is_ready

    @pytest.mark.asyncio
    async def test_client_ready_after_init(self):
        proc = self._make_mock_process()
        client = LSPClient(proc, "python")

        # Mock send_request to return capabilities
        client.send_request = AsyncMock(return_value={"capabilities": {}})
        client._read_task = AsyncMock()
        client._read_task.done.return_value = False

        await client.initialize("file:///tmp")
        assert client.is_ready

    @pytest.mark.asyncio
    async def test_shutdown_calls_terminate(self):
        proc = self._make_mock_process()
        client = LSPClient(proc, "python")
        client._read_task = AsyncMock()
        client._read_task.done.return_value = False
        client.send_request = AsyncMock()

        await client.shutdown()
        proc.terminate.assert_called_once()


# ─── LSPServerManager 测试 ──────────────────────────────────────────

class TestLSPServerManager:
    """测试 LSP Server Manager"""

    def test_initial_state(self):
        manager = LSPServerManager()
        assert len(manager._clients) == 0

    def test_set_root(self):
        manager = LSPServerManager()
        manager.set_root("/tmp/workspace")
        assert manager._root_uri.startswith("file://")

    @pytest.mark.asyncio
    async def test_get_client_unsupported_language(self):
        manager = LSPServerManager()
        with pytest.raises(LSPError, match="Unsupported language"):
            await manager.get_client("rust")

    @pytest.mark.asyncio
    async def test_get_client_command_not_found(self):
        """如果 pylsp 未安装，应抛出 LSPError"""
        manager = LSPServerManager()
        with patch("shutil.which", return_value=None):
            with pytest.raises(LSPError, match="not found"):
                await manager.get_client("python")

    @pytest.mark.asyncio
    async def test_close_all(self):
        manager = LSPServerManager()
        # 添加一个 mock client
        mock_client = AsyncMock()
        manager._clients["python"] = mock_client

        await manager.close_all()
        mock_client.shutdown.assert_called_once()
        assert len(manager._clients) == 0


# ─── 工具类测试 ──────────────────────────────────────────────────────

class TestLSPTools:
    """测试 LSP 工具定义"""

    def test_goto_definition_properties(self):
        tool = LSPGotoDefinitionTool()
        assert tool.name == "lsp_goto_definition"
        assert tool.permission.value == "read"
        assert "path" in tool.parameters["properties"]
        assert "line" in tool.parameters["properties"]
        assert "column" in tool.parameters["properties"]

    def test_find_references_properties(self):
        tool = LSPFindReferencesTool()
        assert tool.name == "lsp_find_references"
        assert tool.permission.value == "read"
        assert "include_declaration" in tool.parameters["properties"]

    def test_hover_properties(self):
        tool = LSPHoverTool()
        assert tool.name == "lsp_hover"
        assert tool.permission.value == "read"

    def test_diagnostics_properties(self):
        tool = LSPDiagnosticsTool()
        assert tool.name == "lsp_diagnostics"
        assert tool.permission.value == "read"
        assert "path" in tool.parameters["properties"]

    def test_symbols_properties(self):
        tool = LSPSymbolsTool()
        assert tool.name == "lsp_symbols"
        assert tool.permission.value == "read"

    @pytest.mark.asyncio
    async def test_goto_definition_unsupported_file(self):
        tool = LSPGotoDefinitionTool()
        result = await tool.execute(path="test.xyz", line=1, column=0)
        assert "Unsupported file type" in result

    @pytest.mark.asyncio
    async def test_find_references_unsupported_file(self):
        tool = LSPFindReferencesTool()
        result = await tool.execute(path="test.xyz", line=1, column=0)
        assert "Unsupported file type" in result

    @pytest.mark.asyncio
    async def test_hover_unsupported_file(self):
        tool = LSPHoverTool()
        result = await tool.execute(path="test.xyz", line=1, column=0)
        assert "Unsupported file type" in result

    @pytest.mark.asyncio
    async def test_diagnostics_unsupported_file(self):
        tool = LSPDiagnosticsTool()
        result = await tool.execute(path="test.xyz")
        assert "Unsupported file type" in result

    @pytest.mark.asyncio
    async def test_symbols_unsupported_file(self):
        tool = LSPSymbolsTool()
        result = await tool.execute(path="test.xyz")
        assert "Unsupported file type" in result

    @pytest.mark.asyncio
    async def test_tool_missing_required_params(self):
        """缺少必填参数应抛出 ToolExecutionError"""
        from coding_agent.tools.base import ToolExecutionError

        tool = LSPGotoDefinitionTool()
        with pytest.raises(ToolExecutionError):
            await tool.execute()

        tool2 = LSPSymbolsTool()
        with pytest.raises(ToolExecutionError):
            await tool2.execute()


# ─── 注册测试 ────────────────────────────────────────────────────────

class TestRegistration:
    """测试工具注册"""

    def test_register_lsp_tools(self):
        from coding_agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        with patch("coding_agent.tools.lsp_ops.register_tool", registry.register):
            register_lsp_tools()

        tool_names = [t.name for t in registry.get_all_tools()]
        assert "lsp_goto_definition" in tool_names
        assert "lsp_find_references" in tool_names
        assert "lsp_hover" in tool_names
        assert "lsp_diagnostics" in tool_names
        assert "lsp_symbols" in tool_names

    def test_all_lsp_tools_are_read_permission(self):
        """所有 LSP 工具都应该是 READ 权限"""
        tools = [
            LSPGotoDefinitionTool(),
            LSPFindReferencesTool(),
            LSPHoverTool(),
            LSPDiagnosticsTool(),
            LSPSymbolsTool(),
        ]
        from coding_agent.tools.base import ToolPermission

        for tool in tools:
            assert tool.permission == ToolPermission.READ, f"{tool.name} should be READ"

    def test_get_server_manager_singleton(self):
        """get_server_manager 应返回同一个实例"""
        import coding_agent.tools.lsp_ops as lsp_mod

        # Reset singleton
        lsp_mod._server_manager = None
        m1 = get_server_manager()
        m2 = get_server_manager()
        assert m1 is m2


# ─── 符号格式化测试 ──────────────────────────────────────────────────

class TestSymbolFormatting:
    """测试符号树格式化"""

    def test_format_flat_symbols(self):
        tool = LSPSymbolsTool()
        symbols = [
            {"name": "Foo", "kind": 5, "range": {"start": {"line": 0, "character": 0}}},
            {"name": "bar", "kind": 12, "range": {"start": {"line": 5, "character": 4}}},
        ]
        output = []
        tool._format_symbols(symbols, output)
        assert "[Class] Foo (line 1, col 0)" in output[0]
        assert "[Function] bar (line 6, col 4)" in output[1]

    def test_format_nested_symbols(self):
        tool = LSPSymbolsTool()
        symbols = [
            {
                "name": "MyClass",
                "kind": 5,
                "range": {"start": {"line": 0, "character": 0}},
                "children": [
                    {"name": "method", "kind": 6, "range": {"start": {"line": 3, "character": 4}}},
                ],
            }
        ]
        output = []
        tool._format_symbols(symbols, output)
        assert len(output) == 2
        assert "MyClass" in output[0]
        assert "method" in output[1]
        # 子符号应该有更深的缩进
        assert output[1].startswith("    ")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
