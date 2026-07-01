"""
后台任务工具 - job_list / job_status / job_cancel

配合 core/jobs.JobRegistry 与 agent_spawn(background=true)：让模型能查询/取消
后台跑的子代理任务。参考 opencode 的 background job（list/get/cancel）。
"""
from __future__ import annotations

import json
from typing import Any

from .base import Tool, ToolPermission
from ..core.jobs import get_job_registry


class JobListTool(Tool):
    """列出所有后台任务及其状态。"""

    @property
    def name(self) -> str:
        return "job_list"

    @property
    def description(self) -> str:
        return "List all background jobs (from agent_spawn background=true) with their status."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        jobs = get_job_registry().list()
        if not jobs:
            return "No background jobs."
        return json.dumps([j.to_dict() for j in jobs], indent=2, ensure_ascii=False)


class JobStatusTool(Tool):
    """查询单个后台任务的状态与结果。"""

    @property
    def name(self) -> str:
        return "job_status"

    @property
    def description(self) -> str:
        return ("Get the status and result of a background job by id. "
                "Returns its status (running/done/failed/cancelled) and, when finished, "
                "its result or error.")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job id (e.g. job-1)"},
            },
            "required": ["job_id"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        job_id = (kwargs.get("job_id") or "").strip()
        if not job_id:
            return "Error: job_id is required"
        job = get_job_registry().get(job_id)
        if job is None:
            return f"Error: job '{job_id}' not found"
        return json.dumps(job.to_dict(), indent=2, ensure_ascii=False)


class JobCancelTool(Tool):
    """取消一个运行中的后台任务。"""

    @property
    def name(self) -> str:
        return "job_cancel"

    @property
    def description(self) -> str:
        return "Cancel a running background job by id."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job id to cancel"},
            },
            "required": ["job_id"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.EXECUTE

    async def execute(self, **kwargs: Any) -> str:
        job_id = (kwargs.get("job_id") or "").strip()
        if not job_id:
            return "Error: job_id is required"
        ok = get_job_registry().cancel(job_id)
        if not ok:
            return f"Could not cancel '{job_id}' (not found or already finished)."
        return f"Cancelled job {job_id}."


def register_job_tools(registry: Any = None) -> None:
    """注册后台任务工具。"""
    from .registry import get_registry

    reg = registry or get_registry()
    reg.register(JobListTool())
    reg.register(JobStatusTool())
    reg.register(JobCancelTool())
