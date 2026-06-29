"""
浏览器控制工具测试

验证 browser_ops 模块中所有工具的基本行为：
- 注册与权限
- 参数校验
- 浏览器生命周期管理
- 各工具功能（需 playwright 已安装）
"""
from __future__ import annotations

import pytest

from coding_agent.tools.base import ToolPermission, ToolExecutionError
from coding_agent.tools.browser_ops import (
    BrowserOpenTool,
    BrowserScreenshotTool,
    BrowserClickTool,
    BrowserTypeTool,
    BrowserEvaluateTool,
    BrowserSnapshotTool,
    BrowserCloseTool,
    register_browser_tools,
    _close_browser,
)
from coding_agent.tools.registry import ToolRegistry


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def registry() -> ToolRegistry:
    """包含浏览器工具的注册中心"""
    reg = ToolRegistry()
    # 直接注册到本地 registry（不走全局单例）
    reg.register(BrowserOpenTool())
    reg.register(BrowserScreenshotTool())
    reg.register(BrowserClickTool())
    reg.register(BrowserTypeTool())
    reg.register(BrowserEvaluateTool())
    reg.register(BrowserSnapshotTool())
    reg.register(BrowserCloseTool())
    return reg


# ── 注册与权限 ───────────────────────────────────────────────────

class TestRegistration:
    """测试工具注册和权限配置"""

    def test_all_tools_registered(self, registry: ToolRegistry):
        tools = registry.get_all_tools()
        names = {t.name for t in tools}
        expected = {
            "browser_open",
            "browser_screenshot",
            "browser_click",
            "browser_type",
            "browser_evaluate",
            "browser_snapshot",
            "browser_close",
        }
        assert expected.issubset(names), f"Missing tools: {expected - names}"

    def test_read_permissions(self, registry: ToolRegistry):
        read_tools = registry.get_tools_by_permission(ToolPermission.READ)
        read_names = {t.name for t in read_tools}
        assert "browser_open" in read_names
        assert "browser_screenshot" in read_names
        assert "browser_snapshot" in read_names

    def test_write_permissions(self, registry: ToolRegistry):
        write_tools = registry.get_tools_by_permission(ToolPermission.WRITE)
        write_names = {t.name for t in write_tools}
        assert "browser_click" in write_names
        assert "browser_type" in write_names

    def test_execute_permissions(self, registry: ToolRegistry):
        exec_tools = registry.get_tools_by_permission(ToolPermission.EXECUTE)
        exec_names = {t.name for t in exec_tools}
        assert "browser_evaluate" in exec_names

    def test_dangerous_permissions(self, registry: ToolRegistry):
        dangerous_tools = registry.get_tools_by_permission(ToolPermission.DANGEROUS)
        dangerous_names = {t.name for t in dangerous_tools}
        assert "browser_close" in dangerous_names

    def test_openai_function_format(self, registry: ToolRegistry):
        tool = registry.get_tool("browser_open")
        assert tool is not None
        func = tool.get_openai_function()
        assert func["name"] == "browser_open"
        assert "parameters" in func
        assert "url" in func["parameters"]["properties"]


# ── 参数校验 ─────────────────────────────────────────────────────

class TestParameterValidation:
    """测试必需参数校验（工具会抛出 ToolExecutionError）"""

    @pytest.mark.asyncio
    async def test_open_requires_url(self):
        tool = BrowserOpenTool()
        with pytest.raises(ToolExecutionError, match="url is required"):
            await tool.execute()

    @pytest.mark.asyncio
    async def test_click_requires_selector(self):
        tool = BrowserClickTool()
        with pytest.raises(ToolExecutionError, match="selector is required"):
            await tool.execute()

    @pytest.mark.asyncio
    async def test_type_requires_selector(self):
        tool = BrowserTypeTool()
        with pytest.raises(ToolExecutionError, match="selector is required"):
            await tool.execute(text="hello")

    @pytest.mark.asyncio
    async def test_type_requires_text(self):
        tool = BrowserTypeTool()
        with pytest.raises(ToolExecutionError, match="text is required"):
            await tool.execute(selector="#input")

    @pytest.mark.asyncio
    async def test_evaluate_requires_expression(self):
        tool = BrowserEvaluateTool()
        with pytest.raises(ToolExecutionError, match="expression is required"):
            await tool.execute()


# ── 工具定义 ─────────────────────────────────────────────────────

class TestToolDefinitions:
    """测试工具元数据完整性"""

    def test_all_tools_have_description(self, registry: ToolRegistry):
        for tool in registry.get_all_tools():
            assert tool.description, f"{tool.name} has no description"

    def test_all_tools_have_parameters(self, registry: ToolRegistry):
        for tool in registry.get_all_tools():
            params = tool.parameters
            assert "type" in params, f"{tool.name} missing 'type' in parameters"

    def test_required_params_marked(self, registry: ToolRegistry):
        tool = registry.get_tool("browser_type")
        assert tool is not None
        params = tool.parameters
        assert "selector" in params.get("required", [])
        assert "text" in params.get("required", [])


# ── 浏览器生命周期 ───────────────────────────────────────────────

class TestBrowserLifecycle:
    """测试浏览器实例管理逻辑"""

    @pytest.mark.asyncio
    async def test_close_without_open_returns_message(self):
        """未打开时关闭应返回正常消息"""
        import coding_agent.tools.browser_ops as mod
        mod._browser_instance = None
        mod._browser_context = None
        result = await _close_browser()
        assert "closed" in result.lower()

    @pytest.mark.asyncio
    async def test_close_tool_permission_is_dangerous(self):
        tool = BrowserCloseTool()
        assert tool.permission == ToolPermission.DANGEROUS


# ── 工具描述一致性 ───────────────────────────────────────────────

class TestToolDescriptions:
    """测试 OpenAI function 格式正确性"""

    def test_browser_open_function(self, registry: ToolRegistry):
        tool = registry.get_tool("browser_open")
        func = tool.get_openai_function()
        assert func["name"] == "browser_open"
        assert "url" in func["parameters"]["properties"]
        assert "url" in func["parameters"]["required"]

    def test_browser_screenshot_function(self, registry: ToolRegistry):
        tool = registry.get_tool("browser_screenshot")
        func = tool.get_openai_function()
        assert func["name"] == "browser_screenshot"

    def test_browser_click_function(self, registry: ToolRegistry):
        tool = registry.get_tool("browser_click")
        func = tool.get_openai_function()
        assert func["name"] == "browser_click"
        assert "selector" in func["parameters"]["required"]

    def test_browser_type_function(self, registry: ToolRegistry):
        tool = registry.get_tool("browser_type")
        func = tool.get_openai_function()
        assert func["name"] == "browser_type"
        required = func["parameters"]["required"]
        assert "selector" in required
        assert "text" in required

    def test_browser_evaluate_function(self, registry: ToolRegistry):
        tool = registry.get_tool("browser_evaluate")
        func = tool.get_openai_function()
        assert func["name"] == "browser_evaluate"
        assert "expression" in func["parameters"]["required"]

    def test_browser_snapshot_function(self, registry: ToolRegistry):
        tool = registry.get_tool("browser_snapshot")
        func = tool.get_openai_function()
        assert func["name"] == "browser_snapshot"

    def test_browser_close_function(self, registry: ToolRegistry):
        tool = registry.get_tool("browser_close")
        func = tool.get_openai_function()
        assert func["name"] == "browser_close"


# ── 通过 registry.execute_tool 集成测试 ─────────────────────────

class TestRegistryIntegration:
    """测试工具通过注册中心执行"""

    @pytest.mark.asyncio
    async def test_registry_execute_unknown_tool(self, registry: ToolRegistry):
        result = await registry.execute_tool("browser_nonexistent", {})
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_registry_execute_browser_close_without_open(self, registry: ToolRegistry):
        import coding_agent.tools.browser_ops as mod
        mod._browser_instance = None
        mod._browser_context = None
        result = await registry.execute_tool("browser_close", {})
        assert "closed" in result.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
