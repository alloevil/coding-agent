"""
测试 TUI 把 error/compact/retry/done 事件映射到状态，以及标题 / plan-mode 表头。
"""
from coding_agent.ui.tui import TuiState, build_header, build_notice, build_footer


def test_header_includes_title_and_plan_mode():
    s = TuiState(model="m", session_id="abc12345", turn=2,
                 title="Fix login bug", plan_mode=True)
    h = build_header(s)
    assert "Fix login bug" in h
    assert "plan-mode" in h
    assert "m" in h


def test_header_without_title():
    s = TuiState(model="m", session_id="abc12345")
    h = build_header(s)
    assert "plan-mode" not in h


def test_notice_empty_default():
    assert build_notice(TuiState()) == ""


def test_footer_new_statuses():
    assert "compacting" in build_footer(TuiState(status="compacting"))
    assert "retrying" in build_footer(TuiState(status="retrying"))


# ---- _apply_event 映射 ----

class _Cfg:
    model = "m"


class _Policy:
    plan_mode = False


class _Loop:
    permission_policy = _Policy()


class _AgentState:
    metadata = {}


class _FakeAgent:
    def __init__(self):
        self.config = _Cfg()
        self.agent_loop = _Loop()
        self.state = _AgentState()

    class _MC:
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_reasoning_tokens = 0
        cache_hit_rate = 0.0
    model_client = _MC()


class _Event:
    def __init__(self, event, data):
        self.event = event
        self.data = data


def _app():
    from coding_agent.ui.app import TuiApp
    return TuiApp(_FakeAgent())


def test_error_event_sets_notice():
    from coding_agent.core import AgentEvent
    app = _app()
    app._apply_event(_Event(AgentEvent.ERROR, {"error": "boom"}))
    assert app.state.status == "error"
    assert "boom" in app.state.notice


def test_compacting_event():
    from coding_agent.core import AgentEvent
    app = _app()
    app._apply_event(_Event(AgentEvent.COMPACTING, {}))
    assert app.state.status == "compacting"
    assert "compacting" in app.state.notice.lower()


def test_retrying_event():
    from coding_agent.core import AgentEvent
    app = _app()
    app._apply_event(_Event(AgentEvent.RETRYING,
                            {"tool_name": "shell_exec", "attempt": 1, "max_retries": 3}))
    assert app.state.status == "retrying"
    assert "shell_exec" in app.state.notice


def test_done_event_sets_turn():
    from coding_agent.core import AgentEvent
    app = _app()
    app._apply_event(_Event(AgentEvent.DONE, {"turns": 7}))
    assert app.state.status == "done"
    assert app.state.turn == 7


def test_thinking_clears_notice():
    from coding_agent.core import AgentEvent
    app = _app()
    app.state.notice = "old error"
    app._apply_event(_Event(AgentEvent.THINKING, {"turn": 1}))
    assert app.state.notice == ""


def test_plan_mode_synced_from_policy():
    from coding_agent.core import AgentEvent
    app = _app()
    app.agent.agent_loop.permission_policy.plan_mode = True
    app._apply_event(_Event(AgentEvent.THINKING, {"turn": 1}))
    assert app.state.plan_mode is True


def test_title_synced_from_metadata():
    from coding_agent.core import AgentEvent
    app = _app()
    app.agent.state.metadata = {"title": "My Session"}
    app._apply_event(_Event(AgentEvent.THINKING, {"turn": 1}))
    assert app.state.title == "My Session"
