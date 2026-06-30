"""
测试 shell_exec 的持久化工作目录
"""
import os
import tempfile
import pytest

from coding_agent.tools.shell import ShellExecTool


@pytest.fixture
def workdir():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "sub"), exist_ok=True)
    return d


@pytest.mark.asyncio
async def test_cd_persists_across_calls(workdir):
    t = ShellExecTool()
    await t.execute(command=f"cd {workdir}/sub")
    assert t._cwd == f"{workdir}/sub"
    out = await t.execute(command="pwd")
    assert f"{workdir}/sub" in out


@pytest.mark.asyncio
async def test_cd_dotdot_persists(workdir):
    t = ShellExecTool()
    await t.execute(command=f"cd {workdir}/sub")
    await t.execute(command="cd ..")
    out = await t.execute(command="pwd")
    # 末段应是 workdir 本身，而不是 sub
    assert out.rstrip().endswith(os.path.basename(workdir))


@pytest.mark.asyncio
async def test_relative_writes_land_in_persisted_cwd(workdir):
    t = ShellExecTool()
    await t.execute(command=f"cd {workdir}")
    await t.execute(command="echo hi > marker.txt")
    assert os.path.exists(os.path.join(workdir, "marker.txt"))


@pytest.mark.asyncio
async def test_sentinel_stripped_from_output(workdir):
    t = ShellExecTool()
    out = await t.execute(command=f"cd {workdir} && echo VISIBLE")
    assert "VISIBLE" in out
    assert "__CWD_AFTER__" not in out  # 哨兵不应出现在展示输出里


@pytest.mark.asyncio
async def test_explicit_workdir_overrides(workdir):
    t = ShellExecTool()
    out = await t.execute(command="pwd", workdir=f"{workdir}/sub")
    assert f"{workdir}/sub" in out
