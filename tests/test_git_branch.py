"""
测试 git_branch 工具：列出 / 创建 / 切换分支。

用一个临时 git 仓库做端到端验证（需要本机有 git）。
"""
import asyncio
import subprocess

import pytest

from coding_agent.tools.git_ops import GitBranchTool


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """初始化一个带有一次提交的临时 git 仓库，并切换工作目录到其中。"""
    if subprocess.run(["git", "--version"],
                      stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode != 0:
        pytest.skip("git not available")
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_list_branches_marks_current(repo):
    out = asyncio.run(GitBranchTool().execute())
    # 默认分支带 "* " 前缀
    assert "*" in out


def test_create_branch_switches(repo):
    out = asyncio.run(GitBranchTool().execute(create="feature-x"))
    assert "feature-x" in out
    listing = asyncio.run(GitBranchTool().execute())
    # 创建后应处于新分支
    assert "* feature-x" in listing


def test_switch_branch(repo):
    asyncio.run(GitBranchTool().execute(create="dev"))
    # 回到原始分支（main 或 master，取决于 git 版本）
    listing = asyncio.run(GitBranchTool().execute())
    other = "master" if "master" in listing else "main"
    out = asyncio.run(GitBranchTool().execute(switch=other))
    assert other in out
    assert f"* {other}" in asyncio.run(GitBranchTool().execute())


def test_switch_nonexistent_branch_errors(repo):
    out = asyncio.run(GitBranchTool().execute(switch="does-not-exist"))
    assert out.lower().startswith("error")


def test_registered():
    from coding_agent.tools.registry import ToolRegistry
    from coding_agent.tools.git_ops import register_git_tools
    reg = ToolRegistry()
    register_git_tools(reg)
    assert reg.get_tool("git_branch") is not None
