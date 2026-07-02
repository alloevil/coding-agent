"""
测试写后失效旧读取：文件被 edit/write/apply_patch 后，更早的 file_read 标记过期。
"""
from coding_agent.context.manager import ContextManager
from coding_agent.core.state import AgentState, ToolCall


def _read(st, cid, path, content):
    st.add_assistant_message(f"read {path}",
                             tool_calls=[ToolCall(id=cid, name="file_read",
                                                  arguments={"path": path})])
    st.add_tool_result(cid, content)


def _edit(st, cid, path):
    st.add_assistant_message(f"edit {path}",
                             tool_calls=[ToolCall(id=cid, name="file_edit",
                                                  arguments={"path": path,
                                                             "old_text": "a", "new_text": "b"})])
    st.add_tool_result(cid, "edited ok")


def _filler(st, n):
    for i in range(n):
        st.add_user_message(f"filler {i}")


def test_read_then_edit_marks_read_stale():
    cm = ContextManager(max_tokens=100000)
    st = AgentState()
    _read(st, "r1", "a.py", "original a content")
    _edit(st, "e1", "a.py")
    _filler(st, 12)  # push past the recent window
    n = cm.invalidate_stale_reads(st)
    assert n == 1
    by_id = {m.tool_result.tool_call_id: m.tool_result.content
             for m in st.messages if m.tool_result}
    assert "stale after a later edit" in by_id["r1"]


def test_read_after_edit_is_fresh():
    cm = ContextManager(max_tokens=100000)
    st = AgentState()
    _edit(st, "e1", "a.py")
    _read(st, "r1", "a.py", "post-edit content")  # read AFTER edit → fresh
    _filler(st, 12)
    n = cm.invalidate_stale_reads(st)
    assert n == 0, "a read after the edit is current, not stale"


def test_apply_patch_multifile_invalidates_each():
    cm = ContextManager(max_tokens=100000)
    st = AgentState()
    _read(st, "r1", "a.py", "a content")
    _read(st, "r2", "b.py", "b content")
    st.add_assistant_message("patch both",
                             tool_calls=[ToolCall(id="p1", name="apply_patch",
                                 arguments={"ops": [{"op": "update", "path": "a.py"},
                                                    {"op": "update", "path": "b.py"}]})])
    st.add_tool_result("p1", "patched")
    _filler(st, 12)
    n = cm.invalidate_stale_reads(st)
    assert n == 2, "both files' earlier reads go stale"


def test_unrelated_file_untouched():
    cm = ContextManager(max_tokens=100000)
    st = AgentState()
    _read(st, "r1", "a.py", "a content")
    _edit(st, "e1", "b.py")  # edit a DIFFERENT file
    _filler(st, 12)
    assert cm.invalidate_stale_reads(st) == 0


def test_idempotent_and_noop_without_writes():
    cm = ContextManager(max_tokens=100000)
    st = AgentState()
    _read(st, "r1", "a.py", "x")
    _filler(st, 12)
    assert cm.invalidate_stale_reads(st) == 0  # no writes at all
    _edit(st, "e1", "a.py")
    _filler(st, 12)
    assert cm.invalidate_stale_reads(st) == 1
    assert cm.invalidate_stale_reads(st) == 0  # idempotent
