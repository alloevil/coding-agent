"""
测试 microcompact 微压缩：本地回收旧的已完成工具输出，保留推理和最近窗口。
"""
from coding_agent.context.manager import ContextManager
from coding_agent.core.state import AgentState, ToolCall


def _state_with_tool_history(n_pairs: int, tool_out_len: int = 2000) -> AgentState:
    """构造 n 对 (assistant tool_call + tool_result) 的历史。"""
    st = AgentState()
    st.add_user_message("do a big task")
    for i in range(n_pairs):
        st.add_assistant_message(f"reasoning step {i}",
                                 tool_calls=[ToolCall(id=f"c{i}", name="shell_exec",
                                                      arguments={"command": "ls"})])
        st.add_tool_result(f"c{i}", "X" * tool_out_len)
    return st


def test_microcompact_elides_old_tool_outputs():
    cm = ContextManager(max_tokens=1000)
    st = _state_with_tool_history(10)
    n = cm.microcompact(st)
    assert n > 0, "should elide some old tool outputs"
    # 被回收的变成占位符
    tool_msgs = [m for m in st.messages if m.tool_result]
    elided = [m for m in tool_msgs if m.tool_result.content == cm._ELIDED]
    assert len(elided) == n


def test_microcompact_keeps_recent_window():
    cm = ContextManager(max_tokens=1000)
    st = _state_with_tool_history(10)
    cm.microcompact(st)
    # 最近 MICROCOMPACT_KEEP_RECENT 条内的 tool_result 不该被动
    recent = st.messages[-cm.MICROCOMPACT_KEEP_RECENT:]
    for m in recent:
        if m.tool_result:
            assert m.tool_result.content != cm._ELIDED, "recent tool output preserved"


def test_microcompact_preserves_assistant_reasoning():
    cm = ContextManager(max_tokens=1000)
    st = _state_with_tool_history(10)
    cm.microcompact(st)
    # assistant 的推理文本一条都不能少
    reasoning = [m for m in st.messages if m.role.value == "assistant"]
    assert len(reasoning) == 10
    assert all("reasoning step" in m.content for m in reasoning)


def test_microcompact_idempotent():
    cm = ContextManager(max_tokens=1000)
    st = _state_with_tool_history(10)
    first = cm.microcompact(st)
    second = cm.microcompact(st)
    assert first > 0
    assert second == 0, "already-elided outputs are not re-processed"


def test_microcompact_skips_short_outputs():
    cm = ContextManager(max_tokens=1000)
    st = _state_with_tool_history(10, tool_out_len=100)  # < MIN_LEN
    n = cm.microcompact(st)
    assert n == 0, "short tool outputs aren't worth eliding"


def test_needs_microcompaction_triggers_earlier_than_full():
    cm = ContextManager(max_tokens=1000)
    st = AgentState()
    # 加内容直到实际 token 估算落在 microcompact(60%) 与 full(90%) 之间。
    while st.get_token_estimate() < int(1000 * 0.65):
        st.add_user_message("word " * 200)
    est = st.get_token_estimate()
    assert 600 <= est < 900, f"estimate {est} should sit between the thresholds"
    assert cm.needs_microcompaction(st) is True   # 过了 60%
    assert cm.needs_compaction(st) is False        # 没到 90%
