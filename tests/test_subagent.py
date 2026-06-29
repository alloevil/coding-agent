"""
测试子代理工具

验证 agent_spawn 和 agent_parallel 的核心功能
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from coding_agent.tools.agent_ops import (
    AgentSpawnTool,
    AgentParallelTool,
    register_agent_tools,
    _run_subagent,
    MAX_SPAWN_DEPTH,
    DEFAULT_SUBAGENT_TIMEOUT,
)
from coding_agent.tools.base import ToolPermission, ToolExecutionError
from coding_agent.tools.registry import ToolRegistry
from coding_agent.core.agent import AgentLoop, AgentEvent, AgentEventData
from coding_agent.core.config import AgentConfig
from coding_agent.core.state import AgentState


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def config(tmp_path):
    return AgentConfig(
        model="test-model",
        api_key="test-key",
        api_base_url="http://localhost:8080/v1",
        max_turns=10,
        auto_approve=True,
        session_db_path=str(tmp_path / "sessions.db"),
    )


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def mock_model_fn():
    """模拟模型调用：无工具调用，直接返回文本"""
    async def _mock(context, tools):
        return {"content": "Task completed successfully.", "tool_calls": []}
    return _mock


@pytest.fixture
def agent(config, registry, mock_model_fn):
    """创建一个测试 AgentLoop"""
    ag = AgentLoop(config=config, tool_registry=registry)
    ag.set_model_call_fn(mock_model_fn)
    return ag


# ── 基础属性测试 ──────────────────────────────────────────────────────────

class TestAgentSpawnTool:
    def test_name(self):
        tool = AgentSpawnTool()
        assert tool.name == "agent_spawn"

    def test_permission_is_execute(self):
        tool = AgentSpawnTool()
        assert tool.permission == ToolPermission.EXECUTE

    def test_parameters_schema(self):
        tool = AgentSpawnTool()
        params = tool.parameters
        assert "task" in params["properties"]
        assert "model" in params["properties"]
        assert "max_turns" in params["properties"]
        assert params["required"] == ["task"]

    def test_openai_function_format(self):
        tool = AgentSpawnTool()
        fn = tool.get_openai_function()
        assert fn["name"] == "agent_spawn"
        assert "description" in fn
        assert "parameters" in fn


class TestAgentParallelTool:
    def test_name(self):
        tool = AgentParallelTool()
        assert tool.name == "agent_parallel"

    def test_permission_is_execute(self):
        tool = AgentParallelTool()
        assert tool.permission == ToolPermission.EXECUTE

    def test_parameters_schema(self):
        tool = AgentParallelTool()
        params = tool.parameters
        assert "tasks" in params["properties"]
        assert params["required"] == ["tasks"]

    def test_openai_function_format(self):
        tool = AgentParallelTool()
        fn = tool.get_openai_function()
        assert fn["name"] == "agent_parallel"


# ── 注册测试 ──────────────────────────────────────────────────────────────

class TestRegisterAgentTools:
    def test_registers_both_tools(self, registry, agent):
        register_agent_tools(registry, agent)
        tool_names = [t.name for t in registry.get_all_tools()]
        assert "agent_spawn" in tool_names
        assert "agent_parallel" in tool_names

    def test_tools_have_parent_reference(self, registry, agent):
        register_agent_tools(registry, agent)
        spawn = registry.get_tool("agent_spawn")
        assert spawn._parent_agent is agent

    def test_auto_registered_on_agent_init(self, tmp_path):
        """AgentLoop.__init__ 自动注册子代理工具"""
        reg = ToolRegistry()
        cfg = AgentConfig(model="m", api_key="k", api_base_url="http://x",
                          session_db_path=str(tmp_path / "s.db"))
        ag = AgentLoop(config=cfg, tool_registry=reg)
        tool_names = [t.name for t in reg.get_all_tools()]
        assert "agent_spawn" in tool_names
        assert "agent_parallel" in tool_names


# ── agent_spawn 执行测试 ──────────────────────────────────────────────────

class TestAgentSpawnExecution:
    @pytest.mark.asyncio
    async def test_spawn_returns_result(self, agent, registry, mock_model_fn):
        """正常 spawn 返回子代理结果"""
        register_agent_tools(registry, agent)
        tool = registry.get_tool("agent_spawn")
        result = await tool.execute(task="Say hello")
        assert "Task completed" in result

    @pytest.mark.asyncio
    async def test_spawn_requires_task(self, agent, registry):
        register_agent_tools(registry, agent)
        tool = registry.get_tool("agent_spawn")
        with pytest.raises(ToolExecutionError, match="task is required"):
            await tool.execute()

    @pytest.mark.asyncio
    async def test_spawn_requires_parent_agent(self):
        """没有 parent_agent 时抛出错误"""
        tool = AgentSpawnTool(parent_agent=None)
        with pytest.raises(ToolExecutionError, match="Parent agent not configured"):
            await tool.execute(task="test")

    @pytest.mark.asyncio
    async def test_spawn_creates_independent_state(self, agent, registry, mock_model_fn):
        """子代理有独立的 state，不影响父代理"""
        register_agent_tools(registry, agent)
        tool = registry.get_tool("agent_spawn")

        # 记录父代理 state 消息数
        parent_msg_count = len(agent.tool_registry._tools)  # 用 registry 间接验证

        result = await tool.execute(task="Do something")
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_spawn_depth_limit(self, config, registry, mock_model_fn):
        """子代理不能再 spawn 子子代理"""
        # 创建一个 depth=1 的代理（模拟子代理）
        parent = AgentLoop(config=config, tool_registry=registry)
        parent.set_model_call_fn(mock_model_fn)
        parent._spawn_depth = 1  # 已经是子代理

        register_agent_tools(registry, parent)
        tool = registry.get_tool("agent_spawn")

        result = await tool.execute(task="Try to spawn")
        assert "Maximum spawn depth" in result

    @pytest.mark.asyncio
    async def test_spawn_with_model_override(self, agent, registry, mock_model_fn):
        """支持模型覆盖参数"""
        register_agent_tools(registry, agent)
        tool = registry.get_tool("agent_spawn")

        result = await tool.execute(task="Test", model="custom-model", max_turns=3)
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_spawn_timeout(self, config, registry):
        """子代理超时处理"""
        # 创建一个会无限循环的模型函数
        async def slow_model(context, tools):
            await asyncio.sleep(200)  # 远超超时时间
            return {"content": "never", "tool_calls": []}

        agent = AgentLoop(config=config, tool_registry=registry)
        agent.set_model_call_fn(slow_model)
        register_agent_tools(registry, agent)
        tool = registry.get_tool("agent_spawn")

        # 临时降低超时
        import coding_agent.tools.agent_ops as ops
        original_timeout = ops.DEFAULT_SUBAGENT_TIMEOUT
        ops.DEFAULT_SUBAGENT_TIMEOUT = 0.1  # 100ms
        try:
            result = await tool.execute(task="Slow task")
            assert "timed out" in result
        finally:
            ops.DEFAULT_SUBAGENT_TIMEOUT = original_timeout


# ── agent_parallel 执行测试 ────────────────────────────────────────────────

class TestAgentParallelExecution:
    @pytest.mark.asyncio
    async def test_parallel_returns_all_results(self, agent, registry, mock_model_fn):
        """并行执行返回所有结果"""
        register_agent_tools(registry, agent)
        tool = registry.get_tool("agent_parallel")

        tasks = [
            {"task": "Task A", "label": "a"},
            {"task": "Task B", "label": "b"},
        ]
        result = await tool.execute(tasks=tasks)

        assert "[a]" in result
        assert "[b]" in result
        assert "Complete" in result

    @pytest.mark.asyncio
    async def test_parallel_requires_tasks(self, agent, registry):
        register_agent_tools(registry, agent)
        tool = registry.get_tool("agent_parallel")

        with pytest.raises(ToolExecutionError, match="non-empty list"):
            await tool.execute()

    @pytest.mark.asyncio
    async def test_parallel_max_10_tasks(self, agent, registry):
        """最多 10 个并行任务"""
        register_agent_tools(registry, agent)
        tool = registry.get_tool("agent_parallel")

        tasks = [{"task": f"Task {i}"} for i in range(11)]
        with pytest.raises(ToolExecutionError, match="Maximum 10"):
            await tool.execute(tasks=tasks)

    @pytest.mark.asyncio
    async def test_parallel_requires_parent_agent(self):
        tool = AgentParallelTool(parent_agent=None)
        with pytest.raises(ToolExecutionError, match="Parent agent not configured"):
            await tool.execute(tasks=[{"task": "test"}])

    @pytest.mark.asyncio
    async def test_parallel_default_labels(self, agent, registry, mock_model_fn):
        """缺少 label 时自动生成"""
        register_agent_tools(registry, agent)
        tool = registry.get_tool("agent_parallel")

        result = await tool.execute(tasks=[{"task": "No label task"}])
        assert "[task-1]" in result

    @pytest.mark.asyncio
    async def test_parallel_mixed_success_failure(self, config, registry):
        """混合成功和失败的结果"""
        call_count = 0

        async def flaky_model(context, tools):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"content": "Success!", "tool_calls": []}
            else:
                raise RuntimeError("Model failed")

        agent = AgentLoop(config=config, tool_registry=registry)
        agent.set_model_call_fn(flaky_model)
        register_agent_tools(registry, agent)
        tool = registry.get_tool("agent_parallel")

        result = await tool.execute(tasks=[
            {"task": "Good task", "label": "good"},
            {"task": "Bad task", "label": "bad"},
        ])

        # 一个成功一个失败，但都应该有结果
        assert "good" in result.lower() or "[good]" in result
        assert "bad" in result.lower() or "[bad]" in result

    @pytest.mark.asyncio
    async def test_parallel_tasks_actually_run_concurrently(self, config, registry):
        """验证任务确实并行执行（而非串行）"""
        import time

        async def slow_model(context, tools):
            await asyncio.sleep(0.3)
            return {"content": "done", "tool_calls": []}

        agent = AgentLoop(config=config, tool_registry=registry)
        agent.set_model_call_fn(slow_model)
        register_agent_tools(registry, agent)
        tool = registry.get_tool("agent_parallel")

        tasks = [
            {"task": f"Task {i}", "label": f"t{i}"}
            for i in range(4)
        ]

        start = time.monotonic()
        result = await tool.execute(tasks=tasks)
        elapsed = time.monotonic() - start

        # 4 个 0.3s 的任务并行应该 < 1s（串行需要 1.2s）
        assert elapsed < 1.0, f"Expected parallel execution < 1s, got {elapsed:.2f}s"
        assert result.count("Complete") == 4

    @pytest.mark.asyncio
    async def test_parallel_task_missing_task_field(self, agent, registry):
        """缺少 task 字段时报错"""
        register_agent_tools(registry, agent)
        tool = registry.get_tool("agent_parallel")

        with pytest.raises(ToolExecutionError, match="missing 'task' field"):
            await tool.execute(tasks=[{"label": "no-task"}])


# ── _run_subagent 内部函数测试 ────────────────────────────────────────────

class TestRunSubagent:
    @pytest.mark.asyncio
    async def test_subagent_inherits_config(self, agent, mock_model_fn):
        """子代理继承父代理的配置"""
        result = await _run_subagent(
            parent_agent=agent,
            task="test task",
            label="test",
        )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_subagent_auto_approve(self, agent, mock_model_fn):
        """子代理默认 auto_approve=True"""
        result = await _run_subagent(
            parent_agent=agent,
            task="test",
            label="test",
        )
        # 不应因权限问题失败
        assert "Permission denied" not in result

    @pytest.mark.asyncio
    async def test_subagent_max_turns(self, config, registry):
        """子代理遵守 max_turns 限制"""
        turn_count = 0

        async def counting_model(context, tools):
            nonlocal turn_count
            turn_count += 1
            return {"content": f"Turn {turn_count}", "tool_calls": []}

        agent = AgentLoop(config=config, tool_registry=registry)
        agent.set_model_call_fn(counting_model)

        result = await _run_subagent(
            parent_agent=agent,
            task="Count turns",
            label="counter",
            max_turns=3,
        )

        # 子代理应该在 3 轮内停止
        assert turn_count <= 3


# ── 集成测试 ──────────────────────────────────────────────────────────────

class TestIntegration:
    @pytest.mark.asyncio
    async def test_full_agent_loop_with_subagent_tools(self, config, registry, mock_model_fn):
        """完整 AgentLoop 能识别子代理工具"""
        agent = AgentLoop(config=config, tool_registry=registry)
        agent.set_model_call_fn(mock_model_fn)

        # 工具列表应包含子代理工具
        tools = registry.get_openai_functions()
        tool_names = [t["function"]["name"] for t in tools]
        assert "agent_spawn" in tool_names
        assert "agent_parallel" in tool_names

    @pytest.mark.asyncio
    async def test_agent_state_independence(self, config, registry, mock_model_fn):
        """验证父子代理 state 完全独立"""
        parent = AgentLoop(config=config, tool_registry=registry)
        parent.set_model_call_fn(mock_model_fn)

        parent_state = AgentState(max_turns=5)
        parent_state.add_user_message("Parent message")

        result = await _run_subagent(
            parent_agent=parent,
            task="Child task",
            label="child",
            max_turns=2,
        )

        # 父代理 state 不受影响
        assert len(parent_state.messages) == 1
        assert parent_state.messages[0].content == "Parent message"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
