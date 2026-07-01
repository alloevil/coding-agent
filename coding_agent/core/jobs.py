"""
后台任务注册表 - 把长任务（子代理、长命令）扔到后台异步跑，不阻塞主循环

设计参考 opencode 的 background/job（job registry：start/list/get/cancel/wait）
与 Codex exec-server 的 RunningProcess map（按 id 管理进程生命周期）——两家本质
相同：一个按 id 索引的任务表，每个任务有状态和结果，支持启动/查询/取消/等待。

我们基于 asyncio：start() 用 create_task 立即返回 job_id 不阻塞；任务完成后
把结果/异常记到 Job 上。纯内存、单例。
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


# 任务状态
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


@dataclass
class Job:
    """一个后台任务的状态快照。"""
    id: str
    label: str
    status: str = RUNNING
    result: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "status": self.status,
                "result": self.result, "error": self.error}


class JobRegistry:
    """按 id 管理后台 asyncio 任务的注册表。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._counter = itertools.count(1)

    def start(self, coro_factory: Callable[[], Awaitable[str]], label: str = "job") -> str:
        """启动一个后台任务，立即返回 job_id（不阻塞）。

        coro_factory 是一个返回 awaitable 的可调用（延迟创建协程，确保在
        当前事件循环里调度）。任务完成后结果/异常写回 Job。
        """
        job_id = f"job-{next(self._counter)}"
        job = Job(id=job_id, label=label)
        self._jobs[job_id] = job

        async def _runner() -> None:
            try:
                result = await coro_factory()
                if job.status != CANCELLED:
                    job.result = result if isinstance(result, str) else str(result)
                    job.status = DONE
            except asyncio.CancelledError:
                job.status = CANCELLED
                raise
            except Exception as e:  # noqa: BLE001
                job.error = f"{type(e).__name__}: {e}"
                job.status = FAILED

        self._tasks[job_id] = asyncio.ensure_future(_runner())
        return job_id

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return list(self._jobs.values())

    def cancel(self, job_id: str) -> bool:
        """取消一个运行中的任务；返回是否成功发起取消。"""
        job = self._jobs.get(job_id)
        task = self._tasks.get(job_id)
        if job is None or task is None:
            return False
        if job.status != RUNNING:
            return False
        job.status = CANCELLED
        task.cancel()
        return True

    async def wait(self, job_id: str, timeout: float | None = None) -> Job | None:
        """等待某任务结束，返回其 Job（超时/不存在返回当前快照或 None）。"""
        task = self._tasks.get(job_id)
        if task is None:
            return self._jobs.get(job_id)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001
            pass  # 异常已在 _runner 里记到 job
        return self._jobs.get(job_id)

    def clear_finished(self) -> int:
        """清理已结束的任务记录，返回清理数量。"""
        finished = [jid for jid, j in self._jobs.items() if j.status != RUNNING]
        for jid in finished:
            self._jobs.pop(jid, None)
            self._tasks.pop(jid, None)
        return len(finished)


_registry: JobRegistry | None = None


def get_job_registry() -> JobRegistry:
    """获取全局 job 注册表单例。"""
    global _registry
    if _registry is None:
        _registry = JobRegistry()
    return _registry


def reset_job_registry() -> None:
    """重置单例（主要供测试隔离用）。"""
    global _registry
    _registry = None
