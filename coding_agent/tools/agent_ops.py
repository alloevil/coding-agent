"""
子代理工具 - 并行任务执行

参考 Claude Code 的子代理设计：
- agent_spawn: 启动单个子代理执行任务
- agent_parallel: 并行启动多个子代理

子代理是独立的 AgentLoop 实例，共享同一个 tool_registry，
有独立的 state（独立对话历史），不能 spawn 子子代理（深度限制 = 1）。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from .base import Tool, ToolPermission, ToolExecutionError
from ..core.state import AgentState
from ..core.config import AgentConfig

# 子代理深度限制
MAX_SPAWN_DEPTH = 1

# 子代理默认超时（秒）
DEFAULT_SUBAGENT_TIMEOUT = 120


class AgentSpawnTool(Tool):
    """
    启动子代理执行任务

    子代理是独立的 AgentLoop 实例，共享父代理的 tool_registry，
    但有独立的 state（独立对话历史）。

    子代理不能 spawn 子子代理（深度限制 = 1）。
    """

    def __init__(self, parent_agent: Any = None) -> None:
        """
        Args:
            parent_agent: 父 AgentLoop 实例，用于获取 config 和 tool_registry
        """
        self._parent_agent = parent_agent

    @property
    def name(self) -> str:
        return "agent_spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a sub-agent to execute a task independently. "
            "The sub-agent has its own conversation history but shares the same tool registry. "
            "Use this for tasks that can run independently, like editing a single file or "
            "running a sequence of commands. The sub-agent result is returned when complete."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task description for the sub-agent to execute"
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for the sub-agent (defaults to parent's model)"
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Maximum turns for the sub-agent (default: 10)"
                }
            },
            "required": ["task"]
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.EXECUTE

    async def execute(self, **kwargs: Any) -> str:
        task = kwargs.get("task")
        if not task:
            raise ToolExecutionError(self.name, "task is required")

        model = kwargs.get("model")
        max_turns = kwargs.get("max_turns", 10)

        if self._parent_agent is None:
            raise ToolExecutionError(self.name, "Parent agent not configured")

        return await _run_subagent(
            parent_agent=self._parent_agent,
            task=task,
            label="subagent",
            model=model,
            max_turns=max_turns,
        )


class AgentParallelTool(Tool):
    """
    并行启动多个子代理

    接收一个任务列表，为每个任务创建独立的子代理，
    使用 asyncio.gather 并行执行，返回所有结果的汇总。

    每个子代理不能 spawn 子子代理（深度限制 = 1）。
    """

    def __init__(self, parent_agent: Any = None) -> None:
        self._parent_agent = parent_agent

    @property
    def name(self) -> str:
        return "agent_parallel"

    @property
    def description(self) -> str:
        return (
            "Spawn multiple sub-agents in parallel to execute independent tasks simultaneously. "
            "Each sub-agent has its own conversation history and runs independently. "
            "Results are collected and returned as a summary when all agents complete."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "List of tasks to execute in parallel",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "Task description for this sub-agent"
                            },
                            "label": {
                                "type": "string",
                                "description": "Short label to identify this task in the results"
                            }
                        },
                        "required": ["task"]
                    }
                }
            },
            "required": ["tasks"]
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.EXECUTE

    async def execute(self, **kwargs: Any) -> str:
        tasks = kwargs.get("tasks")
        if not tasks or not isinstance(tasks, list):
            raise ToolExecutionError(self.name, "tasks must be a non-empty list")

        if self._parent_agent is None:
            raise ToolExecutionError(self.name, "Parent agent not configured")

        if len(tasks) > 10:
            raise ToolExecutionError(self.name, "Maximum 10 parallel tasks allowed")

        # 构建并行协程
        coroutines = []
        for i, task_def in enumerate(tasks):
            task_desc = task_def.get("task")
            if not task_desc:
                raise ToolExecutionError(self.name, f"Task {i} is missing 'task' field")
            label = task_def.get("label", f"task-{i+1}")
            coroutines.append(_run_subagent(
                parent_agent=self._parent_agent,
                task=task_desc,
                label=label,
            ))

        # 并行执行
        results = await asyncio.gather(*coroutines, return_exceptions=True)

        # 汇总结果
        summary_parts = []
        for i, result in enumerate(results):
            label = tasks[i].get("label", f"task-{i+1}")
            if isinstance(result, Exception):
                summary_parts.append(f"## [{label}] ❌ Error\n{str(result)}")
            else:
                summary_parts.append(f"## [{label}] ✅ Complete\n{result}")

        return "\n\n".join(summary_parts)


async def _run_subagent(
    parent_agent: Any,
    task: str,
    label: str = "subagent",
    model: str | None = None,
    max_turns: int = 10,
) -> str:
    """
    运行单个子代理

    子代理是独立的 AgentLoop 实例：
    - 共享 parent 的 tool_registry
    - 独立的 AgentState（独立对话历史）
    - 深度限制：子代理不能再 spawn 子代理
    - 超时限制：默认 120 秒

    Args:
        parent_agent: 父 AgentLoop 实例
        task: 子代理任务描述
        label: 标识标签
        model: 可选的模型覆盖
        max_turns: 最大轮次

    Returns:
        子代理执行结果文本

    Raises:
        ToolExecutionError: 执行失败时
    """
    from ..core.agent import AgentLoop

    # 检查深度限制
    parent_depth = getattr(parent_agent, "_spawn_depth", 0)
    if parent_depth >= MAX_SPAWN_DEPTH:
        return f"Error [{label}]: Maximum spawn depth ({MAX_SPAWN_DEPTH}) reached. Sub-agents cannot spawn sub-sub-agents."

    # 创建子代理的 config（继承父代理配置）
    parent_config = parent_agent.config
    child_config = AgentConfig(
        model=model or parent_config.model,
        api_key=parent_config.api_key,
        api_base_url=parent_config.api_base_url,
        max_tokens=parent_config.max_tokens,
        temperature=parent_config.temperature,
        max_context_tokens=parent_config.max_context_tokens,
        auto_compact=parent_config.auto_compact,
        auto_approve=True,  # 子代理自动批准所有操作
        max_turns=max_turns,
        system_prompt=parent_config.system_prompt,
        session_db_path=parent_config.session_db_path,
    )

    # 创建独立的 state
    child_state = AgentState(max_turns=max_turns)

    # 创建子代理 AgentLoop（共享 tool_registry）
    # 注意：不能通过 __init__ 创建，因为会重新注册 agent_tools 覆盖父代理的引用
    child_agent = object.__new__(AgentLoop)
    child_agent.config = child_config
    child_agent.tool_registry = parent_agent.tool_registry
    child_agent._spawn_depth = parent_depth + 1
    child_agent._model_call_fn = None
    child_agent._permission_handler = None

    # 延迟初始化 session_store 和 context_manager
    from ..memory.session import SessionStore
    from ..context.manager import ContextManager
    child_agent.session_store = SessionStore(db_path=child_config.session_db_path)
    child_agent.context_manager = ContextManager(max_tokens=child_config.max_context_tokens)

    # 注入模型调用函数（继承父代理的）
    if parent_agent._model_call_fn:
        child_agent.set_model_call_fn(parent_agent._model_call_fn)

    # 收集最终结果
    final_result = ""

    try:
        # 带超时执行
        async def _collect_result() -> str:
            result_parts = []
            async for event_data in child_agent.run(child_state, user_input=task):
                if event_data.event.value == "assistant_message":
                    result_parts.append(event_data.data.get("content", ""))
                elif event_data.event.value == "error":
                    result_parts.append(f"Error: {event_data.data.get('error', 'Unknown error')}")
                elif event_data.event.value == "done":
                    break
            return "\n".join(result_parts) if result_parts else "Task completed with no output."

        final_result = await asyncio.wait_for(
            _collect_result(),
            timeout=DEFAULT_SUBAGENT_TIMEOUT,
        )

    except asyncio.TimeoutError:
        final_result = f"Error [{label}]: Sub-agent timed out after {DEFAULT_SUBAGENT_TIMEOUT}s"
    except Exception as e:
        final_result = f"Error [{label}]: {str(e)}"

    return final_result


def register_agent_tools(registry: Any, parent_agent: Any) -> None:
    """
    注册子代理工具到工具注册中心

    Args:
        registry: ToolRegistry 实例
        parent_agent: 父 AgentLoop 实例
    """
    registry.register(AgentSpawnTool(parent_agent=parent_agent))
    registry.register(AgentParallelTool(parent_agent=parent_agent))
