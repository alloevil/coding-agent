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
                "agent": {
                    "type": "string",
                    "description": "Optional named agent profile (from .coding-agent/agents/<name>.md) "
                                   "to apply its system prompt, model, and tool restrictions."
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
        agent_name = kwargs.get("agent")

        if self._parent_agent is None:
            raise ToolExecutionError(self.name, "Parent agent not configured")

        return await _run_subagent(
            parent_agent=self._parent_agent,
            task=task,
            label=agent_name or "subagent",
            model=model,
            max_turns=max_turns,
            profile_name=agent_name,
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
    profile_name: str | None = None,
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

    # 解析命名 agent profile（若指定）：覆盖 model / system_prompt / temperature。
    profile = None
    if profile_name:
        try:
            from ..core.agent_profiles import load_agent
            profile = load_agent(profile_name)
        except Exception:
            profile = None
        if profile is None:
            return (f"Error [{label}]: agent profile '{profile_name}' not found. "
                    f"Define it at .coding-agent/agents/{profile_name}.md")

    child_config = AgentConfig(
        model=model or (profile.model if profile and profile.model else parent_config.model),
        api_key=parent_config.api_key,
        api_base_url=parent_config.api_base_url,
        max_tokens=parent_config.max_tokens,
        temperature=(profile.temperature if profile and profile.temperature is not None
                     else parent_config.temperature),
        max_context_tokens=parent_config.max_context_tokens,
        auto_compact=parent_config.auto_compact,
        auto_approve=True,  # 子代理自动批准所有操作
        max_turns=max_turns,
        system_prompt=(profile.system_prompt if profile and profile.system_prompt
                       else parent_config.system_prompt),
        session_db_path=parent_config.session_db_path,
    )

    # 创建独立的 state
    child_state = AgentState(max_turns=max_turns)

    # 创建子代理 AgentLoop（共享 tool_registry）。
    # register_builtin_tools=False：不重新注册 agent_spawn/agent_parallel，
    # 保留父代理在共享 registry 中的引用。其余字段（_interrupt_event、
    # retry_config、rollback_log 等）由 __init__ 正常初始化。
    child_agent = AgentLoop(
        config=child_config,
        tool_registry=parent_agent.tool_registry,
        register_builtin_tools=False,
        spawn_depth=parent_depth + 1,
    )

    # 注入模型调用函数（继承父代理的）
    if parent_agent._model_call_fn:
        child_agent.set_model_call_fn(parent_agent._model_call_fn)

    # 应用 profile 的工具过滤（若有）：子代理只能看到/调用允许的工具。
    if profile is not None and (profile.allow_tools or profile.deny_tools):
        child_agent.set_tool_filter(profile.tool_allowed)

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
