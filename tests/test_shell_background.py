"""
测试 shell_exec 的 background 参数：立即返回 job id，任务在 JobRegistry 里跑。
"""
import asyncio

import pytest

from coding_agent.tools.shell import ShellExecTool
from coding_agent.core.jobs import get_job_registry, reset_job_registry, DONE


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_job_registry()
    yield
    reset_job_registry()


class _FakeSandbox:
    """假沙箱：run() 返回固定输出（不真跑命令）。"""
    def __init__(self, output="done", delay=0.0):
        self.output = output
        self.delay = delay
        self.calls = []

    async def run(self, command, workdir=None, timeout=None):
        self.calls.append(command)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.output


async def test_background_returns_job_id_immediately():
    tool = ShellExecTool()
    tool._sandbox = _FakeSandbox(output="server up", delay=0.05)
    result = await tool.execute(command="sleep 100 && echo hi", background=True)
    # 立刻返回 job 引用，而不是命令输出
    assert "background job" in result
    assert "job_status" in result
    # 任务在注册表里
    reg = get_job_registry()
    assert len(reg.list()) == 1


async def test_background_job_completes_and_captures_output():
    tool = ShellExecTool()
    tool._sandbox = _FakeSandbox(output="finished")
    result = await tool.execute(command="build", background=True)
    job_id = result.split("background job ")[1].split(" ")[0]
    reg = get_job_registry()
    job = await reg.wait(job_id, timeout=2)
    assert job is not None
    assert job.status == DONE
    assert job.result == "finished"


async def test_foreground_still_blocks_and_returns_output():
    tool = ShellExecTool()
    tool._sandbox = _FakeSandbox(output="hello\n__CWD_AFTER__:/tmp")
    # 非 background：直接返回输出（sentinel 逻辑照旧）
    out = await tool.execute(command="echo hello", background=False)
    assert "hello" in out
    assert get_job_registry().list() == []
