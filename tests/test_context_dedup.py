"""
测试工具结果语义去重：同一文件被 file_read 多次，只留最新，旧的替换占位符。
"""
from coding_agent.context.manager import ContextManager
from coding_agent.core.state import AgentState, ToolCall


def _read(st: AgentState, cid: str, path: str, content: str):
    st.add_assistant_message(f"reading {path}",
                             tool_calls=[ToolCall(id=cid, name="file_read",
                                                  arguments={"path": path})])
    st.add_tool_result(cid, content)


def test_dedup_supersedes_earlier_reads_of_same_file():
    cm = ContextManager(max_tokens=100000)
    st = AgentState()
    _read(st, "c1", "a.py", "OLD content of a")
    _read(st, "c2", "b.py", "content of b")
    # 拉开距离，避免命中「最近窗口保护」
    for i in range(10):
        st.add_user_message(f"filler {i}")
    _read(st, "c3", "a.py", "NEW content of a")
    for i in range(10):
        st.add_user_message(f"filler2 {i}")

    n = cm.dedup_file_reads(st)
    assert n == 1, "the earlier a.py read is superseded"
    # 找 c1 的结果 → 占位符；c3（最新 a.py）保留
    by_id = {m.tool_result.tool_call_id: m.tool_result.content
             for m in st.messages if m.tool_result}
    assert "superseded" in by_id["c1"]
    assert by_id["c3"] == "NEW content of a"
    assert by_id["c2"] == "content of b"  # 不同文件，不动


def test_dedup_keeps_recent_window():
    cm = ContextManager(max_tokens=100000)
    st = AgentState()
    _read(st, "c1", "a.py", "first read")
    _read(st, "c2", "a.py", "second read")  # 都在最近窗口内
    n = cm.dedup_file_reads(st)
    assert n == 0, "recent reads are protected even if same file"


def test_dedup_idempotent():
    cm = ContextManager(max_tokens=100000)
    st = AgentState()
    _read(st, "c1", "a.py", "OLD")
    for i in range(10):
        st.add_user_message(f"filler {i}")
    _read(st, "c2", "a.py", "NEW")
    for i in range(10):
        st.add_user_message(f"filler2 {i}")
    assert cm.dedup_file_reads(st) == 1
    assert cm.dedup_file_reads(st) == 0  # 第二次无新增


def test_dedup_no_file_reads_is_noop():
    cm = ContextManager(max_tokens=100000)
    st = AgentState()
    st.add_user_message("hello")
    st.add_assistant_message("hi")
    assert cm.dedup_file_reads(st) == 0
