"""
测试 apply_patch 可被 rollback_last 撤销
"""
import pytest

from coding_agent.core.agent import AgentLoop
from coding_agent.core.config import AgentConfig
from coding_agent.core.state import AgentState, ToolCall
from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.file_ops import register_file_tools
from coding_agent.tools.patch_ops import register_patch_tools


def _agent(tmp_path):
    cfg = AgentConfig(model="m", api_key="k", auto_approve=True,
                      session_db_path=str(tmp_path / "s.db"))
    reg = ToolRegistry()
    register_file_tools(reg)
    register_patch_tools(reg)
    return AgentLoop(config=cfg, tool_registry=reg)


@pytest.mark.asyncio
async def test_apply_patch_rollback_restores_and_deletes(tmp_path):
    agent = _agent(tmp_path)
    existing = tmp_path / "keep.py"
    existing.write_text("old = 1\n")

    patch = "\n".join([
        "*** Begin Patch",
        "*** Add File: new.py",
        "+created = True",
        "*** Update File: keep.py",
        "@@",
        "-old = 1",
        "+old = 2",
        "*** End Patch",
    ])
    args = {"patch": patch, "root": str(tmp_path)}

    # 走 _execute_with_recovery，使其记录 rollback 快照
    tc = ToolCall(id="1", name="apply_patch", arguments=args)
    result, is_error = await agent._execute_with_recovery(tc, AgentState())
    assert not is_error
    assert (tmp_path / "new.py").exists()
    assert "old = 2" in existing.read_text()

    # 回滚
    msg = agent.rollback_last()
    assert "Rolled back apply_patch" in msg
    assert not (tmp_path / "new.py").exists()       # 新增文件被删
    assert existing.read_text() == "old = 1\n"        # 修改被还原
