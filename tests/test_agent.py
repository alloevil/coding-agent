"""
测试 Agent Loop

验证核心功能是否正常工作
"""
import pytest
import asyncio
from coding_agent.core import AgentState, AgentConfig, AgentLoop
from coding_agent.tools import get_registry
from coding_agent.tools.file_ops import register_file_tools
from coding_agent.tools.shell import register_shell_tools
from coding_agent.tools.git_ops import register_git_tools


@pytest.fixture
def config():
    """测试配置"""
    return AgentConfig(
        model="test-model",
        api_key="test-key",
        api_base_url="http://localhost:8080/v1",
        max_turns=5,
        auto_approve=True
    )


@pytest.fixture
def registry():
    """测试工具注册中心"""
    from coding_agent.tools.registry import ToolRegistry
    reg = ToolRegistry()
    register_file_tools()
    register_shell_tools()
    register_git_tools()
    return reg


@pytest.fixture
def state():
    """测试状态"""
    return AgentState()


def test_state_creation(state):
    """测试状态创建"""
    assert state.turn_count == 0
    assert len(state.messages) == 0
    assert state.session_id is None


def test_state_add_messages(state):
    """测试添加消息"""
    state.add_user_message("Hello")
    assert len(state.messages) == 1
    assert state.messages[0].role.value == "user"
    
    state.add_assistant_message("Hi there!")
    assert len(state.messages) == 2
    assert state.messages[1].role.value == "assistant"


def test_state_turn_count(state):
    """测试轮次计数"""
    assert not state.should_stop()
    
    for _ in range(100):
        state.increment_turn()
    
    assert state.should_stop()


def test_registry_has_tools(registry):
    """测试工具注册"""
    tools = registry.get_all_tools()
    tool_names = [t.name for t in tools]
    
    assert "file_read" in tool_names
    assert "file_write" in tool_names
    assert "file_edit" in tool_names
    assert "shell_exec" in tool_names
    assert "git_status" in tool_names


def test_registry_permission_filter(registry):
    """测试权限过滤"""
    from coding_agent.tools.base import ToolPermission
    
    read_tools = registry.get_tools_by_permission(ToolPermission.READ)
    read_names = [t.name for t in read_tools]
    
    assert "file_read" in read_names
    assert "file_write" not in read_names  # WRITE 权限


@pytest.mark.asyncio
async def test_file_read_tool():
    """测试文件读取工具"""
    import tempfile
    import os
    
    from coding_agent.tools.file_ops import FileReadTool
    
    tool = FileReadTool()
    
    # 创建临时文件
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("Hello\nWorld\n")
        temp_path = f.name
    
    try:
        result = await tool.execute(path=temp_path)
        assert "Hello" in result
        assert "World" in result
    finally:
        os.unlink(temp_path)


@pytest.mark.asyncio
async def test_shell_exec_tool():
    """测试 Shell 执行工具"""
    from coding_agent.tools.shell import ShellExecTool
    
    tool = ShellExecTool()
    
    result = await tool.execute(command="echo 'test'")
    assert "test" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
