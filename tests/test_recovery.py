"""
测试流式中断恢复和错误恢复机制

覆盖：
1. 流式中断恢复（interrupt）
2. 错误分类与重试（retry）
3. 工具执行回滚（rollback）
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coding_agent.core.agent import (
    AgentLoop,
    AgentEvent,
    AgentEventData,
    RetryConfig,
    RollbackRecord,
    RollbackLog,
    _classify_error,
)
from coding_agent.core.state import AgentState, ToolCall
from coding_agent.core.config import AgentConfig
from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.base import Tool, ToolPermission, ToolExecutionError


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def config() -> AgentConfig:
    return AgentConfig(
        model="test-model",
        api_key="test-key",
        auto_approve=True,
        max_turns=10,
    )


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.fixture
def agent(config: AgentConfig, registry: ToolRegistry) -> AgentLoop:
    return AgentLoop(config=config, tool_registry=registry)


# ===========================================================================
# 1. 错误分类测试
# ===========================================================================


class TestClassifyError:
    """测试错误分类函数"""

    def test_transient_timeout(self):
        assert _classify_error("Connection timed out") == "transient"

    def test_transient_network(self):
        assert _classify_error("Network error ECONNREFUSED") == "transient"

    def test_transient_rate_limit(self):
        assert _classify_error("Rate limit exceeded (429)") == "transient"

    def test_transient_busy(self):
        assert _classify_error("Server is busy, try again later") == "transient"

    def test_permanent_not_found(self):
        assert _classify_error("File does not exist: /tmp/foo") == "permanent"

    def test_permanent_permission(self):
        assert _classify_error("Permission denied") == "permanent"

    def test_permanent_syntax(self):
        assert _classify_error("SyntaxError: invalid syntax") == "permanent"

    def test_unknown_error(self):
        assert _classify_error("Something weird happened") == "unknown"

    def test_permanent_takes_priority(self):
        """permanent 关键词优先于 transient"""
        # "not found" 是 permanent，"timeout" 是 transient
        assert _classify_error("File not found after timeout") == "permanent"


# ===========================================================================
# 2. RollbackLog 测试
# ===========================================================================


class TestRollbackLog:
    """测试回滚日志"""

    def test_add_and_pop(self):
        log = RollbackLog(max_records=5)
        rec = RollbackRecord(
            tool_name="file_write",
            arguments={"path": "/tmp/test"},
            rollback_data={"original_content": "old"},
        )
        log.add(rec)
        assert log.last is not None
        assert log.last.tool_name == "file_write"

        popped = log.pop_last()
        assert popped is not None
        assert popped.tool_name == "file_write"
        assert log.pop_last() is None

    def test_max_records_eviction(self):
        log = RollbackLog(max_records=3)
        for i in range(5):
            log.add(RollbackRecord(
                tool_name=f"tool_{i}",
                arguments={},
                rollback_data={},
            ))
        assert len(log.records) == 3
        assert log.records[0].tool_name == "tool_2"  # 最早的被淘汰

    def test_pop_empty(self):
        log = RollbackLog()
        assert log.pop_last() is None
        assert log.last is None


# ===========================================================================
# 3. RetryConfig 测试
# ===========================================================================


class TestRetryConfig:
    """测试重试配置"""

    def test_defaults(self):
        cfg = RetryConfig()
        assert cfg.max_retries == 3
        assert cfg.base_delay == 1.0
        assert cfg.backoff_factor == 2.0

    def test_custom(self):
        cfg = RetryConfig(max_retries=5, base_delay=0.5)
        assert cfg.max_retries == 5
        assert cfg.base_delay == 0.5


# ===========================================================================
# 4. AgentLoop 中断测试
# ===========================================================================


class TestInterrupt:
    """测试流式中断恢复"""

    def test_interrupt_sets_flag(self, agent: AgentLoop):
        assert not agent.is_interrupted()
        agent.interrupt()
        assert agent.is_interrupted()

    def test_clear_interrupt(self, agent: AgentLoop):
        agent.interrupt()
        agent.clear_interrupt()
        assert not agent.is_interrupted()

    @pytest.mark.asyncio
    async def test_run_resets_interrupt(self, agent: AgentLoop):
        """run() 开始时应自动清除中断状态"""
        agent.interrupt()
        assert agent.is_interrupted()

        # run() 需要 model_call_fn，这里只验证状态重置
        # 用 mock 让 run 快速结束
        async def mock_model(ctx, tools):
            return {"content": "done", "tool_calls": []}

        agent.set_model_call_fn(mock_model)
        state = AgentState()

        async for _ in agent.run(state, "test"):
            pass
        assert not agent.is_interrupted()

    @pytest.mark.asyncio
    async def test_interrupt_during_tool_execution(self, agent: AgentLoop, registry: ToolRegistry):
        """中断正在执行的工具，应返回中断标记"""

        class SlowTool(Tool):
            @property
            def name(self) -> str:
                return "slow_tool"

            @property
            def description(self) -> str:
                return "A slow tool"

            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                # 模拟耗时操作
                await asyncio.sleep(10)
                return "completed"

        registry.register(SlowTool())

        # 在后台启动中断
        async def trigger_interrupt():
            await asyncio.sleep(0.1)
            agent.interrupt()

        asyncio.create_task(trigger_interrupt())

        result = await agent._interruptible_execute("slow_tool", {})
        assert "Interrupted" in result

    @pytest.mark.asyncio
    async def test_interrupt_already_set_skips_execution(self, agent: AgentLoop):
        """中断标志已设置时，不执行工具"""
        agent.interrupt()
        result = await agent._interruptible_execute("any_tool", {})
        assert result == "__INTERRUPTED__"

    @pytest.mark.asyncio
    async def test_interrupt_preserves_state(self, agent: AgentLoop, registry: ToolRegistry):
        """中断后 agent 状态不变，可继续接收新输入"""

        class QuickTool(Tool):
            @property
            def name(self) -> str:
                return "quick_tool"

            @property
            def description(self) -> str:
                return "Quick"

            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                return "ok"

        registry.register(QuickTool())

        state = AgentState()
        state.add_user_message("first message")
        msg_count_before = len(state.messages)

        # 中断
        agent.interrupt()
        result = await agent._interruptible_execute("quick_tool", {})
        # 当中断标志已设置时，_interruptible_execute 返回 __INTERRUPTED__
        # （这是内部标记，_execute_with_retry 会将其转为用户友好的消息）
        assert result == "__INTERRUPTED__"

        # 状态未变（interruptible_execute 不修改 state）
        # 但我们可以继续添加消息
        state.add_user_message("second message")
        assert len(state.messages) == msg_count_before + 1


# ===========================================================================
# 5. 错误恢复重试测试
# ===========================================================================


class TestRetryRecovery:
    """测试错误恢复与重试"""

    @pytest.mark.asyncio
    async def test_transient_error_retries(self, agent: AgentLoop, registry: ToolRegistry):
        """瞬态错误应自动重试"""
        call_count = 0

        class FlakyTool(Tool):
            @property
            def name(self) -> str:
                return "flaky_tool"

            @property
            def description(self) -> str:
                return "Fails first 2 times"

            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    return "Error: Connection timed out"
                return "success"

        registry.register(FlakyTool())
        # 设置很短的延迟以加速测试
        agent.retry_config = RetryConfig(max_retries=3, base_delay=0.01, backoff_factor=1.0)

        result, is_error = await agent._execute_with_retry("flaky_tool", {})
        assert result == "success"
        assert not is_error
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_permanent_error_no_retry(self, agent: AgentLoop, registry: ToolRegistry):
        """永久错误不应重试"""
        call_count = 0

        class BrokenTool(Tool):
            @property
            def name(self) -> str:
                return "broken_tool"

            @property
            def description(self) -> str:
                return "Always permanent error"

            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                nonlocal call_count
                call_count += 1
                return "Error: File does not exist: /nonexistent"

        registry.register(BrokenTool())
        agent.retry_config = RetryConfig(max_retries=3, base_delay=0.01)

        result, is_error = await agent._execute_with_retry("broken_tool", {})
        assert is_error
        assert call_count == 1  # 没有重试

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self, agent: AgentLoop, registry: ToolRegistry):
        """超过最大重试次数后返回最终错误"""

        class AlwaysFailTool(Tool):
            @property
            def name(self) -> str:
                return "always_fail"

            @property
            def description(self) -> str:
                return "Always transient fail"

            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                return "Error: Connection timed out"

        registry.register(AlwaysFailTool())
        agent.retry_config = RetryConfig(max_retries=2, base_delay=0.01, backoff_factor=1.0)

        result, is_error = await agent._execute_with_retry("always_fail", {})
        assert is_error
        assert "3 attempts" in result  # max_retries=2 -> 总共 3 次

    @pytest.mark.asyncio
    async def test_exception_transient_retries(self, agent: AgentLoop, registry: ToolRegistry):
        """工具抛出瞬态异常也应重试"""
        call_count = 0

        class ExceptionTool(Tool):
            @property
            def name(self) -> str:
                return "exception_tool"

            @property
            def description(self) -> str:
                return "Throws transient exception"

            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    raise ConnectionError("ECONNREFUSED")
                return "recovered"

        registry.register(ExceptionTool())
        agent.retry_config = RetryConfig(max_retries=3, base_delay=0.01, backoff_factor=1.0)

        result, is_error = await agent._execute_with_retry("exception_tool", {})
        assert result == "recovered"
        assert not is_error
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_interrupt_during_retry(self, agent: AgentLoop, registry: ToolRegistry):
        """重试过程中被中断"""

        class FailTool(Tool):
            @property
            def name(self) -> str:
                return "fail_tool"

            @property
            def description(self) -> str:
                return "Always fails"

            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                return "Error: busy"

        registry.register(FailTool())
        agent.retry_config = RetryConfig(max_retries=5, base_delay=0.5, backoff_factor=1.0)

        # 在重试延迟期间触发中断
        async def trigger_interrupt():
            await asyncio.sleep(0.05)
            agent.interrupt()

        asyncio.create_task(trigger_interrupt())

        result, is_error = await agent._execute_with_retry("fail_tool", {})
        assert "Interrupted" in result
        assert is_error


# ===========================================================================
# 6. 工具回滚测试
# ===========================================================================


class TestRollback:
    """测试工具执行回滚"""

    def test_rollback_file_write_existing(self, agent: AgentLoop):
        """回滚写入已存在的文件"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("original content")
            path = f.name

        try:
            # 模拟回滚记录
            agent.rollback_log.add(RollbackRecord(
                tool_name="file_write",
                arguments={"path": path, "content": "new content"},
                rollback_data={"path": path, "original_content": "original content"},
            ))

            # 写入新内容
            Path(path).write_text("new content")
            assert Path(path).read_text() == "new content"

            # 回滚
            result = agent.rollback_last()
            assert "Rolled back" in result
            assert Path(path).read_text() == "original content"
        finally:
            os.unlink(path)

    def test_rollback_file_write_new_file(self, agent: AgentLoop):
        """回滚写入新文件（应删除）"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "new_file.txt")

            # 写入新文件
            Path(path).write_text("brand new")
            assert Path(path).exists()

            # 模拟回滚记录
            agent.rollback_log.add(RollbackRecord(
                tool_name="file_write",
                arguments={"path": path, "content": "brand new"},
                rollback_data={"path": path, "original_content": None},
            ))

            # 回滚
            result = agent.rollback_last()
            assert "deleted" in result
            assert not Path(path).exists()

    def test_rollback_file_edit(self, agent: AgentLoop):
        """回滚文件编辑"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("alpha beta gamma\n")
            path = f.name

        try:
            # 执行编辑: beta -> delta
            content = Path(path).read_text()
            new_content = content.replace("beta", "delta")
            Path(path).write_text(new_content)
            assert "delta" in Path(path).read_text()
            assert "beta" not in Path(path).read_text()

            # 模拟回滚记录
            agent.rollback_log.add(RollbackRecord(
                tool_name="file_edit",
                arguments={"path": path, "old_text": "beta", "new_text": "delta"},
                rollback_data={"path": path, "old_text": "beta", "new_text": "delta"},
            ))

            # 回滚
            result = agent.rollback_last()
            assert "Rolled back" in result
            assert "beta" in Path(path).read_text()
            assert "delta" not in Path(path).read_text()
        finally:
            os.unlink(path)

    def test_rollback_shell_exec(self, agent: AgentLoop):
        """回滚 shell 命令（只能返回警告）"""
        agent.rollback_log.add(RollbackRecord(
            tool_name="shell_exec",
            arguments={"command": "rm -rf /tmp/test"},
            rollback_data={"command": "rm -rf /tmp/test", "workdir": None},
        ))

        result = agent.rollback_last()
        assert "Cannot rollback" in result
        assert "Manual intervention" in result

    def test_rollback_empty(self, agent: AgentLoop):
        """没有可回滚的操作"""
        result = agent.rollback_last()
        assert "No tool execution to rollback" in result

    def test_rollback_unsupported_tool(self, agent: AgentLoop):
        """不支持回滚的工具"""
        agent.rollback_log.add(RollbackRecord(
            tool_name="unknown_tool",
            arguments={},
            rollback_data={},
        ))
        result = agent.rollback_last()
        assert "not supported" in result

    @pytest.mark.asyncio
    async def test_rollback_tool_registered(self, agent: AgentLoop, registry: ToolRegistry):
        """rollback_last 工具已注册"""
        tool = registry.get_tool("rollback_last")
        assert tool is not None
        assert tool.name == "rollback_last"

    @pytest.mark.asyncio
    async def test_rollback_record_on_write(self, agent: AgentLoop, registry: ToolRegistry):
        """执行 WRITE 工具前自动记录回滚信息"""

        class MockWriteTool(Tool):
            @property
            def name(self) -> str:
                return "mock_write"

            @property
            def description(self) -> str:
                return "Mock write"

            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {}}

            @property
            def permission(self) -> ToolPermission:
                return ToolPermission.WRITE

            async def execute(self, **kwargs) -> str:
                return "wrote"

        registry.register(MockWriteTool())

        # _execute_with_recovery 应该记录回滚
        tc = ToolCall(id="test", name="mock_write", arguments={"path": "/tmp/test"})
        state = AgentState()
        result, is_error = await agent._execute_with_recovery(tc, state)
        # mock_write 不是 file_write/file_edit/shell_exec，所以不记录回滚
        assert len(agent.rollback_log.records) == 0


# ===========================================================================
# 7. 完整集成测试
# ===========================================================================


class TestIntegration:
    """集成测试：中断 + 重试 + 回滚的组合场景"""

    @pytest.mark.asyncio
    async def test_retry_then_succeed_with_rollback(self, agent: AgentLoop, registry: ToolRegistry):
        """重试成功后，回滚记录应该存在"""
        call_count = 0

        class RetryWriteTool(Tool):
            @property
            def name(self) -> str:
                return "file_write"

            @property
            def description(self) -> str:
                return "Write"

            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}

            @property
            def permission(self) -> ToolPermission:
                return ToolPermission.WRITE

            async def execute(self, **kwargs) -> str:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return "Error: Connection timed out"
                # 实际写入
                path = kwargs.get("path", "")
                content = kwargs.get("content", "")
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text(content)
                return f"Wrote to {path}"

        registry.register(RetryWriteTool())
        agent.retry_config = RetryConfig(max_retries=2, base_delay=0.01, backoff_factor=1.0)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test.txt")
            tc = ToolCall(
                id="test",
                name="file_write",
                arguments={"path": path, "content": "hello"},
            )
            state = AgentState()

            result, is_error = await agent._execute_with_recovery(tc, state)
            assert not is_error
            assert call_count == 2
            # 回滚记录应该存在
            assert len(agent.rollback_log.records) == 1
            assert agent.rollback_log.last.tool_name == "file_write"

    @pytest.mark.asyncio
    async def test_full_agent_loop_with_tools(self, agent: AgentLoop, registry: ToolRegistry):
        """完整 agent loop：工具调用 + 成功"""

        class EchoTool(Tool):
            @property
            def name(self) -> str:
                return "echo"

            @property
            def description(self) -> str:
                return "Echo input"

            @property
            def parameters(self) -> dict:
                return {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                }

            async def execute(self, **kwargs) -> str:
                return kwargs.get("text", "")

        registry.register(EchoTool())

        turn = 0

        async def mock_model(ctx, tools):
            nonlocal turn
            turn += 1
            if turn == 1:
                return {
                    "content": "I'll echo hello",
                    "tool_calls": [{
                        "id": "tc1",
                        "function": {
                            "name": "echo",
                            "arguments": json.dumps({"text": "hello world"}),
                        }
                    }]
                }
            return {"content": "Done!", "tool_calls": []}

        agent.set_model_call_fn(mock_model)
        state = AgentState()

        events = []
        async for event in agent.run(state, "echo hello"):
            events.append(event)

        event_types = [e.event for e in events]
        assert AgentEvent.THINKING in event_types
        assert AgentEvent.TOOL_CALL in event_types
        assert AgentEvent.TOOL_RESULT in event_types
        assert AgentEvent.DONE in event_types

        # 验证工具结果
        tool_results = [e for e in events if e.event == AgentEvent.TOOL_RESULT]
        assert tool_results[0].data["result"] == "hello world"
        assert not tool_results[0].data["is_error"]
