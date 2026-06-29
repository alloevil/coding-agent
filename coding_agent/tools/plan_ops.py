"""
计划/待办工具 - update_plan

参考 Codex 的 update_plan 与 Claude Code 的 TodoWrite：
- 模型用一个工具维护整张计划（全量替换，幂等，无需引用步骤 ID）
- 每个步骤有 status: pending / in_progress / completed
- 约定：同一时刻至多一个 in_progress 步骤，保持聚焦
- 计划存储在 AgentState.metadata["plan"] 上，随会话持久化

该工具改善多步骤 / 规划类任务的完成率：把隐式推理外显成可见的、
可逐步勾选的清单，减少模型“做到一半忘了还有什么”的情况。
"""
from __future__ import annotations

from typing import Any

from .base import Tool, ToolPermission

VALID_STATUSES = ("pending", "in_progress", "completed")

# 存放计划的 metadata 键
PLAN_METADATA_KEY = "plan"


class UpdatePlanTool(Tool):
    """创建或更新当前任务的分步计划。"""

    def __init__(self, state: Any = None) -> None:
        # state: AgentState，用于持久化计划。允许延后注入。
        self._state = state

    def bind_state(self, state: Any) -> None:
        self._state = state

    @property
    def name(self) -> str:
        return "update_plan"

    @property
    def description(self) -> str:
        return (
            "Create or update a step-by-step plan for the current task. "
            "Submit the FULL plan each time (this replaces the previous plan). "
            "Use this for any task with multiple steps: lay out the steps first, "
            "then update statuses as you make progress. Exactly one step should be "
            "'in_progress' at a time. Mark steps 'completed' as you finish them. "
            "Statuses: pending, in_progress, completed."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "The complete ordered list of plan steps.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step": {
                                "type": "string",
                                "description": "Short description of the step",
                            },
                            "status": {
                                "type": "string",
                                "enum": list(VALID_STATUSES),
                                "description": "pending | in_progress | completed",
                            },
                        },
                        "required": ["step", "status"],
                    },
                },
                "explanation": {
                    "type": "string",
                    "description": "Optional one-line note on why the plan changed",
                },
            },
            "required": ["steps"],
        }

    @property
    def permission(self) -> ToolPermission:
        # 纯内部状态更新，无副作用，自动允许
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        steps = kwargs.get("steps")
        if not isinstance(steps, list) or not steps:
            return "Error: 'steps' must be a non-empty list"

        normalized: list[dict[str, str]] = []
        for i, raw in enumerate(steps):
            if not isinstance(raw, dict):
                return f"Error: step {i} must be an object with 'step' and 'status'"
            desc = raw.get("step")
            status = raw.get("status", "pending")
            if not desc or not isinstance(desc, str):
                return f"Error: step {i} is missing a 'step' description"
            if status not in VALID_STATUSES:
                return (
                    f"Error: step {i} has invalid status '{status}'. "
                    f"Must be one of {VALID_STATUSES}"
                )
            normalized.append({"step": desc, "status": status})

        in_progress = [s for s in normalized if s["status"] == "in_progress"]
        if len(in_progress) > 1:
            return (
                "Error: at most one step may be 'in_progress' at a time "
                f"(found {len(in_progress)})"
            )

        # 持久化到 state
        if self._state is not None:
            self._state.metadata[PLAN_METADATA_KEY] = normalized

        return render_plan(normalized)


def render_plan(steps: list[dict[str, str]]) -> str:
    """把计划渲染成带勾选框的可读文本。"""
    symbols = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    lines = ["Plan updated:"]
    done = 0
    for s in steps:
        sym = symbols.get(s["status"], "[ ]")
        if s["status"] == "completed":
            done += 1
        lines.append(f"  {sym} {s['step']}")
    lines.append(f"  ({done}/{len(steps)} completed)")
    return "\n".join(lines)


def register_plan_tools(state: Any = None, registry: Any = None) -> UpdatePlanTool:
    """
    注册计划工具。返回工具实例，便于之后用 bind_state 注入会话状态。
    """
    from .registry import get_registry

    reg = registry or get_registry()
    tool = UpdatePlanTool(state=state)
    reg.register(tool)
    return tool
