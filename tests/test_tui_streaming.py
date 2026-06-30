"""
测试 TUI 流式缓冲与回调接线（修复 print 打断 Live 的 bug）。
"""
from coding_agent.ui.tui import TuiState, build_transcript


def test_stream_text_accumulates():
    s = TuiState()
    s.stream_text("Hel")
    s.stream_text("lo")
    assert s.live_text == "Hello"


def test_live_text_shown_in_transcript():
    s = TuiState()
    s.add_user("hi")
    s.stream_text("thinking out loud")
    t = build_transcript(s)
    assert "thinking out loud" in t
    assert "▌" in t  # 进行中光标


def test_add_assistant_commits_live_buffer():
    s = TuiState()
    s.stream_text("streamed answer")
    s.add_assistant("")  # 最终内容为空 → 用流式缓冲
    assert s.messages[-1] == {"role": "assistant", "text": "streamed answer"}
    assert s.live_text == ""  # 提交后清空


def test_add_assistant_prefers_final_text():
    s = TuiState()
    s.stream_text("partial")
    s.add_assistant("final complete text")
    assert s.messages[-1]["text"] == "final complete text"
    assert s.live_text == ""


def test_reasoning_buffer_cleared_on_commit():
    s = TuiState()
    s.stream_reasoning("let me think")
    s.add_assistant("done")
    assert s.live_reasoning == ""


def test_app_callbacks_redirect_to_buffer():
    # TuiApp 的 _on_text_delta 应写入 state.live_text，而不是 print
    from coding_agent.ui.app import TuiApp

    class _Cfg:
        model = "m"

    class _FakeAgent:
        config = _Cfg()

    app = TuiApp(_FakeAgent())
    app._on_text_delta("abc")
    app._on_reasoning_delta("xyz")
    assert app.state.live_text == "abc"
    assert app.state.live_reasoning == "xyz"


def test_refresh_noop_without_live():
    from coding_agent.ui.app import TuiApp

    class _Cfg:
        model = "m"

    class _FakeAgent:
        config = _Cfg()

    app = TuiApp(_FakeAgent())
    # _live 为 None 时 _refresh 不应抛出
    app._refresh()
