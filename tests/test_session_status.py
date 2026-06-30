"""
测试会话状态广播（SessionStatusTracker）。
"""
from coding_agent.core.session_status import (
    SessionStatusTracker, SessionStatus,
    IDLE, THINKING, RUNNING_TOOL, COMPACTING, RETRYING, ERROR, DONE,
)


def test_initial_idle():
    t = SessionStatusTracker(session_id="s1")
    assert t.status.state == IDLE
    assert t.status.session_id == "s1"


def test_thinking_sets_turn():
    t = SessionStatusTracker()
    t.update_from_event("thinking", {"turn": 3})
    assert t.status.state == THINKING
    assert t.status.turn == 3


def test_tool_call_then_result():
    t = SessionStatusTracker()
    t.update_from_event("tool_call", {"name": "grep"})
    assert t.status.state == RUNNING_TOOL
    assert t.status.current_tool == "grep"
    t.update_from_event("tool_result", {})
    assert t.status.current_tool is None


def test_error_records_message():
    t = SessionStatusTracker()
    t.update_from_event("error", {"error": "boom"})
    assert t.status.state == ERROR
    assert t.status.last_error == "boom"


def test_done_sets_turns():
    t = SessionStatusTracker()
    t.update_from_event("done", {"turns": 7})
    assert t.status.state == DONE
    assert t.status.turn == 7


def test_compacting_and_retrying():
    t = SessionStatusTracker()
    t.update_from_event("compacting", {})
    assert t.status.state == COMPACTING
    t.update_from_event("retrying", {})
    assert t.status.state == RETRYING


def test_thinking_clears_error():
    t = SessionStatusTracker()
    t.update_from_event("error", {"error": "x"})
    t.update_from_event("thinking", {"turn": 1})
    assert t.status.last_error is None


def test_subscribe_notified():
    t = SessionStatusTracker()
    seen = []
    t.subscribe(lambda s: seen.append((s.state, s.turn)))
    t.update_from_event("thinking", {"turn": 2})
    t.update_from_event("done", {"turns": 2})
    assert (THINKING, 2) in seen
    assert (DONE, 2) in seen


def test_unsubscribe():
    t = SessionStatusTracker()
    seen = []
    unsub = t.subscribe(lambda s: seen.append(s.state))
    t.update_from_event("thinking", {"turn": 1})
    unsub()
    t.update_from_event("done", {"turns": 1})
    assert seen == [THINKING]  # done 之后已退订


def test_subscriber_exception_isolated():
    t = SessionStatusTracker()
    def boom(s):
        raise RuntimeError("nope")
    t.subscribe(boom)
    # 不应抛出
    t.update_from_event("thinking", {"turn": 1})
    assert t.status.state == THINKING


def test_set_usage_notifies():
    t = SessionStatusTracker()
    seen = []
    t.subscribe(lambda s: seen.append((s.prompt_tokens, s.completion_tokens)))
    t.set_usage(100, 20)
    assert seen[-1] == (100, 20)


def test_unknown_event_no_notify():
    t = SessionStatusTracker()
    seen = []
    t.subscribe(lambda s: seen.append(s.state))
    t.update_from_event("permission_request", {})
    assert seen == []  # 未知事件不通知


def test_render_and_to_dict():
    t = SessionStatusTracker(session_id="s1")
    t.update_from_event("tool_call", {"name": "shell_exec"})
    t.set_usage(50, 10)
    r = t.render()
    assert "running_tool" in r and "shell_exec" in r and "50 in" in r
    d = t.status.to_dict()
    assert d["state"] == RUNNING_TOOL and d["session_id"] == "s1"
