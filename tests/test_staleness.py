"""
测试读后被改的陈旧检测（advisory）
"""
import os
import pytest

from coding_agent.tools.file_ops import FileReadTool, FileEditTool
import coding_agent.tools.file_ops as fo


@pytest.mark.asyncio
async def test_edit_after_external_change_warns(tmp_path):
    f = tmp_path / "c.py"
    f.write_text("x = 1\n")
    await FileReadTool().execute(path=str(f))
    # 模拟外部修改：内容+mtime 都变
    f.write_text("x = 1\ny = 2\n")
    os.utime(f, (f.stat().st_atime + 100, f.stat().st_mtime + 100))
    out = await FileEditTool().execute(path=str(f), old_text="x = 1", new_text="x = 9")
    assert "changed on disk" in out


@pytest.mark.asyncio
async def test_edit_without_external_change_no_warning(tmp_path):
    f = tmp_path / "c.py"
    f.write_text("x = 1\n")
    await FileReadTool().execute(path=str(f))
    out = await FileEditTool().execute(path=str(f), old_text="x = 1", new_text="x = 9")
    assert "changed on disk" not in out


@pytest.mark.asyncio
async def test_edit_without_prior_read_no_warning(tmp_path):
    f = tmp_path / "fresh.py"
    f.write_text("a = 1\n")
    # 没读过 -> 不提醒
    out = await FileEditTool().execute(path=str(f), old_text="a = 1", new_text="a = 2")
    assert "changed on disk" not in out


def test_record_and_staleness_helpers(tmp_path):
    f = tmp_path / "h.txt"
    f.write_text("hi\n")
    fo._record_read(str(f))
    assert fo._staleness_warning(str(f)) == ""
    f.write_text("hi there\n")
    os.utime(f, (f.stat().st_atime + 50, f.stat().st_mtime + 50))
    assert "changed on disk" in fo._staleness_warning(str(f))
