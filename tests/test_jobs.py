"""
测试后台任务注册表（core/jobs.py）。
"""
import asyncio

import pytest

from coding_agent.core.jobs import (
    JobRegistry, RUNNING, DONE, FAILED, CANCELLED,
    get_job_registry, reset_job_registry,
)


def test_start_returns_id_immediately():
    async def main():
        reg = JobRegistry()

        async def work():
            await asyncio.sleep(0.05)
            return "result-value"

        jid = reg.start(work, label="t1")
        # 立即返回，任务还在跑
        assert jid.startswith("job-")
        assert reg.get(jid).status == RUNNING
        # 等它完成
        job = await reg.wait(jid, timeout=2)
        assert job.status == DONE
        assert job.result == "result-value"
    asyncio.run(main())


def test_failed_job_records_error():
    async def main():
        reg = JobRegistry()

        async def boom():
            raise ValueError("nope")

        jid = reg.start(boom, label="bad")
        job = await reg.wait(jid, timeout=2)
        assert job.status == FAILED
        assert "ValueError" in job.error and "nope" in job.error
    asyncio.run(main())


def test_cancel_running_job():
    async def main():
        reg = JobRegistry()

        async def slow():
            await asyncio.sleep(10)
            return "never"

        jid = reg.start(slow, label="slow")
        assert reg.cancel(jid) is True
        job = await reg.wait(jid, timeout=2)
        assert job.status == CANCELLED
        # 已结束的不能再取消
        assert reg.cancel(jid) is False
    asyncio.run(main())


def test_cancel_unknown_job():
    reg = JobRegistry()
    assert reg.cancel("job-999") is False


def test_list_jobs():
    async def main():
        reg = JobRegistry()

        async def quick():
            return "ok"

        j1 = reg.start(quick, label="a")
        j2 = reg.start(quick, label="b")
        await reg.wait(j1, timeout=2)
        await reg.wait(j2, timeout=2)
        jobs = reg.list()
        assert len(jobs) == 2
        assert {j.label for j in jobs} == {"a", "b"}
    asyncio.run(main())


def test_wait_timeout_returns_snapshot():
    async def main():
        reg = JobRegistry()

        async def slow():
            await asyncio.sleep(10)
            return "x"

        jid = reg.start(slow, label="slow")
        job = await reg.wait(jid, timeout=0.05)  # 超时
        assert job.status == RUNNING  # 还在跑
        reg.cancel(jid)
    asyncio.run(main())


def test_clear_finished():
    async def main():
        reg = JobRegistry()

        async def quick():
            return "ok"

        async def slow():
            await asyncio.sleep(10)

        j1 = reg.start(quick, label="done")
        j2 = reg.start(slow, label="running")
        await reg.wait(j1, timeout=2)
        n = reg.clear_finished()
        assert n == 1
        assert reg.get(j1) is None
        assert reg.get(j2) is not None
        reg.cancel(j2)
    asyncio.run(main())


def test_singleton():
    reset_job_registry()
    a = get_job_registry()
    b = get_job_registry()
    assert a is b
    reset_job_registry()
    assert get_job_registry() is not a
