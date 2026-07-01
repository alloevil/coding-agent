"""
测试后台任务工具 job_list/status/cancel + agent_spawn(background=true)。
"""
import asyncio

import pytest

from coding_agent.core.jobs import get_job_registry, reset_job_registry
from coding_agent.tools.job_ops import (
    JobListTool, JobStatusTool, JobCancelTool, register_job_tools,
)


def _seed_job(label="t", result="ok", delay=0.0):
    reg = get_job_registry()

    async def work():
        if delay:
            await asyncio.sleep(delay)
        return result
    return reg.start(work, label=label)


def test_job_list_empty():
    reset_job_registry()
    out = asyncio.run(JobListTool().execute())
    assert "No background jobs" in out


def test_job_list_and_status():
    async def main():
        reset_job_registry()
        jid = _seed_job(label="build", result="done!")
        await get_job_registry().wait(jid, timeout=2)
        lst = await JobListTool().execute()
        assert "build" in lst and jid in lst
        st = await JobStatusTool().execute(job_id=jid)
        assert "done" in st and "done!" in st
    asyncio.run(main())


def test_job_status_unknown():
    reset_job_registry()
    out = asyncio.run(JobStatusTool().execute(job_id="job-999"))
    assert out.lower().startswith("error") and "not found" in out


def test_job_status_missing_id():
    out = asyncio.run(JobStatusTool().execute(job_id=""))
    assert out.lower().startswith("error")


def test_job_cancel():
    async def main():
        reset_job_registry()
        jid = _seed_job(label="slow", delay=10)
        out = await JobCancelTool().execute(job_id=jid)
        assert "Cancelled" in out
        # 再取消已结束的 → 失败提示
        await get_job_registry().wait(jid, timeout=1)
        out2 = await JobCancelTool().execute(job_id=jid)
        assert "Could not cancel" in out2
    asyncio.run(main())


def test_register():
    from coding_agent.tools.registry import ToolRegistry
    reg = ToolRegistry()
    register_job_tools(reg)
    for name in ("job_list", "job_status", "job_cancel"):
        assert reg.get_tool(name) is not None


def test_background_spawn_returns_job_id():
    """agent_spawn(background=true) 立即返回 job_id，不阻塞。"""
    async def main():
        reset_job_registry()
        from coding_agent.tools.agent_ops import AgentSpawnTool

        # 假父 agent：_run_subagent 会用到 config/_model_call_fn/tool_registry/_spawn_depth
        class _Cfg:
            model = "m"; api_key = "k"; api_base_url = ""; max_tokens = 100
            temperature = None; max_context_tokens = 1000; auto_compact = False
            session_db_path = ":memory:"; system_prompt = "x"
        class _Parent:
            config = _Cfg(); _spawn_depth = 0; tool_registry = None
            _model_call_fn = None

        tool = AgentSpawnTool(parent_agent=_Parent())
        out = await tool.execute(task="do something", background=True)
        assert "Started background job" in out
        assert "job-" in out
        # 注册表里确实有这个 job
        assert len(get_job_registry().list()) == 1
    asyncio.run(main())
