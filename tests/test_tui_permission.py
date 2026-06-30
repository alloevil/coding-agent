"""
测试 TUI 在交互式提问（权限 / ask_user）期间暂停 Live，避免和 input() 抢终端。
"""
import asyncio


class _Cfg:
    model = "m"


class _FakeLive:
    def __init__(self):
        self.events = []

    def stop(self):
        self.events.append("stop")

    def start(self, refresh=False):
        self.events.append("start")

    def update(self, *a, **k):
        self.events.append("update")


class _FakeAgent:
    def __init__(self):
        self.config = _Cfg()
        self.confirm_calls = []
        self.ask_calls = []

    async def _confirm_permission(self, tool, args):
        self.confirm_calls.append((tool, args))
        return True

    async def _ask_user(self, q, opts):
        self.ask_calls.append((q, opts))
        return "answer"


def _make_app():
    from coding_agent.ui.app import TuiApp
    return TuiApp(_FakeAgent())


def test_confirm_permission_suspends_live():
    app = _make_app()
    live = _FakeLive()
    app._live = live
    ok = asyncio.run(app._confirm_permission("file_write", {"path": "x"}))
    assert ok is True
    # Live 应在提问前 stop、之后 start
    assert live.events == ["stop", "start"]
    assert app.agent.confirm_calls == [("file_write", {"path": "x"})]


def test_ask_user_suspends_live():
    app = _make_app()
    live = _FakeLive()
    app._live = live
    ans = asyncio.run(app._ask_user("Pick?", ["a", "b"]))
    assert ans == "answer"
    assert live.events == ["stop", "start"]


def test_suspend_live_noop_without_live():
    app = _make_app()
    # 没有活动 Live 时也应正常委托，不抛异常
    ok = asyncio.run(app._confirm_permission("shell_exec", {"command": "ls"}))
    assert ok is True


def test_suspend_restarts_live_even_on_exception():
    app = _make_app()
    live = _FakeLive()
    app._live = live

    async def boom(tool, args):
        raise RuntimeError("nope")

    app.agent._confirm_permission = boom
    try:
        asyncio.run(app._confirm_permission("x", {}))
    except RuntimeError:
        pass
    # 即使委托抛异常，Live 也必须重启
    assert live.events == ["stop", "start"]
