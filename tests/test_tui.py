"""
测试 TUI 渲染逻辑（纯函数 + 状态机 + compose）
"""
from coding_agent.ui.tui import (
    TuiState, ToolCallView, build_header, build_footer, build_plan_panel,
    build_transcript,
)


def test_tool_call_view_line():
    tc = ToolCallView(id="1", name="file_read", arguments={"path": "a.py"})
    assert "⏳" in tc.line() and "file_read" in tc.line()
    tc.status = "ok"; tc.result = "contents"
    assert "✅" in tc.line() and "contents" in tc.line()


def test_state_user_assistant_flow():
    s = TuiState(model="m")
    s.add_user("hello")
    s.add_assistant("hi there")
    s.add_assistant("")  # 空助手消息被忽略
    assert len(s.messages) == 2
    assert s.messages[0] == {"role": "user", "text": "hello"}


def test_state_tool_lifecycle():
    s = TuiState()
    s.start_tool("1", "grep", {"pattern": "x"})
    assert s.tool_calls[0].status == "running"
    s.finish_tool("1", "found", is_error=False)
    assert s.tool_calls[0].status == "ok" and s.tool_calls[0].result == "found"
    s.start_tool("2", "shell", {})
    s.finish_tool("2", "boom", is_error=True)
    assert s.tool_calls[1].status == "error"


def test_build_header():
    s = TuiState(model="gpt-5-mini", session_id="abcdef123456", turn=3)
    h = build_header(s)
    assert "gpt-5-mini" in h and "abcdef12" in h and "turn 3" in h


def test_build_plan_panel():
    s = TuiState(plan=[{"step": "a", "status": "completed"},
                       {"step": "b", "status": "in_progress"}])
    p = build_plan_panel(s)
    assert "[x] a" in p and "[~] b" in p and "(1/2)" in p


def test_build_plan_empty():
    assert build_plan_panel(TuiState()) == ""


def test_build_footer():
    s = TuiState(prompt_tokens=100, completion_tokens=20, reasoning_tokens=5,
                 cache_hit_rate=0.5, status="thinking")
    f = build_footer(s)
    assert "thinking" in f and "100 in" in f and "reasoning 5" in f and "cache 50%" in f


def test_build_transcript_limits():
    s = TuiState()
    for i in range(20):
        s.add_user(f"msg{i}")
    t = build_transcript(s, max_messages=5)
    assert "msg19" in t and "msg0" not in t


def test_compose_returns_renderable():
    from coding_agent.ui.app import _compose
    s = TuiState(model="m", session_id="s1")
    s.add_user("hi")
    s.start_tool("1", "grep", {"pattern": "x"})
    s.plan = [{"step": "do", "status": "in_progress"}]
    group = _compose(s)
    # rich Group 应可被 console 渲染（不抛异常）
    from rich.console import Console
    Console(file=open("/dev/null", "w")).print(group)
