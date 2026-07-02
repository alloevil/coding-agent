"""
测试 EditJournal + /undo：写类工具记录先前内容，/undo 恢复最近一次改动。
"""
from coding_agent.core.edit_journal import (
    EditJournal, get_edit_journal, reset_edit_journal,
)
from coding_agent.core.commands import dispatch, CommandContext


def test_undo_reverts_overwrite(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("original")
    j = EditJournal()
    j.record(str(f))            # 写前记录
    f.write_text("modified")    # 模拟工具改动
    msg = j.undo_last()
    assert "Reverted" in msg
    assert f.read_text() == "original"


def test_undo_deletes_newly_created(tmp_path):
    f = tmp_path / "new.txt"
    j = EditJournal()
    j.record(str(f))            # 文件此刻不存在
    f.write_text("created")     # 工具新建
    msg = j.undo_last()
    assert "deleted" in msg
    assert not f.exists()


def test_undo_is_lifo(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("a0"); b.write_text("b0")
    j = EditJournal()
    j.record(str(a)); a.write_text("a1")
    j.record(str(b)); b.write_text("b1")
    j.undo_last()  # 撤销 b（最近）
    assert b.read_text() == "b0" and a.read_text() == "a1"
    j.undo_last()  # 再撤销 a
    assert a.read_text() == "a0"


def test_undo_empty_is_safe():
    j = EditJournal()
    assert not j.can_undo()
    assert "Nothing to undo" in j.undo_last()


def test_undo_command_dispatches_action():
    r = dispatch("/undo", CommandContext(tool_names=[]))
    assert r.kind == "action" and r.payload == "undo"


def test_file_write_records_to_global_journal(tmp_path):
    """file_write 真的会往全局日志记录（集成）。"""
    import asyncio
    from coding_agent.tools.file_ops import FileWriteTool
    reset_edit_journal()
    f = tmp_path / "w.txt"
    f.write_text("v1")

    async def run():
        tool = FileWriteTool()
        await tool.execute(path=str(f), content="v2")
    asyncio.run(run())
    assert f.read_text() == "v2"
    # /undo 应还原
    msg = get_edit_journal().undo_last()
    assert f.read_text() == "v1", msg
    reset_edit_journal()
