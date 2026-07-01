"""
测试 protocol.py 的并发 stdin：turn 运行期间提交的 interrupt 能即时处理，
不必等 turn 结束（这是键盘 Esc 中断的前提）。
"""
import asyncio

import pytest


class _FakeLoop:
    """假 AgentLoop：run() 慢慢 yield，interrupt() 记录被调用。"""
    def __init__(self):
        self.interrupted = asyncio.Event()
        self.permission_policy = type("P", (), {"plan_mode": False})()

    def set_model_call_fn(self, fn): pass
    def set_token_usage_fn(self, fn): pass
    def set_permission_handler(self, fn): pass

    def interrupt(self):
        self.interrupted.set()

    async def run(self, state, content):
        # 慢 turn：给 interrupt 留时间
        for i in range(20):
            if self.interrupted.is_set():
                return
            await asyncio.sleep(0.02)
            yield type("Ev", (), {"event": None, "data": {}})()


def _make_protocol(monkeypatch):
    """构造一个 AgentProtocol，但把重依赖替换成假的。"""
    from coding_agent import protocol as P

    # 避免真的初始化工具/模型/存储
    monkeypatch.setattr(P, "register_file_tools", lambda *a, **k: None)
    monkeypatch.setattr(P, "register_shell_tools", lambda *a, **k: None)
    monkeypatch.setattr(P, "register_git_tools", lambda *a, **k: None)

    proto = P.AgentProtocol.__new__(P.AgentProtocol)
    proto.config = type("C", (), {"auto_approve": True, "model": "m",
                                  "session_db_path": ":memory:"})()
    proto._turn_task = None
    proto.state = type("S", (), {"session_id": "s", "turn_count": 0})()
    proto.plan_tool = type("PT", (), {"bind_state": lambda self, s: None})()
    proto.agent_loop = _FakeLoop()
    proto.session_store = type("SS", (), {
        "load_state": lambda self, sid: None,
        "create_session": lambda self: "s",
    })()
    proto.tool_registry = type("TR", (), {"get_all_tools": lambda self: []})()
    proto._events = []
    proto._send_event = lambda t, d=None: proto._events.append((t, d or {}))
    return proto


def test_interrupt_during_turn_is_prompt(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        # 启动一个 turn（作为任务，不阻塞）
        await proto.handle_request({"type": "user_input", "content": "go"})
        assert proto._turn_task is not None
        # turn 正在跑；此刻提交 interrupt
        await asyncio.sleep(0.03)
        await proto.handle_request({"type": "interrupt"})
        # interrupt 应已即时调用 agent_loop.interrupt()
        assert proto.agent_loop.interrupted.is_set()
        # 等 turn 收尾
        await asyncio.wait_for(proto._turn_task, timeout=2)
    asyncio.run(main())


def test_turn_task_cleared_after_done(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        await proto.handle_request({"type": "user_input", "content": "go"})
        await proto.handle_request({"type": "interrupt"})
        await asyncio.wait_for(proto._turn_task, timeout=2) if proto._turn_task else None
        # _run_turn 结束后应把 _turn_task 置回 None
        await asyncio.sleep(0.05)
        assert proto._turn_task is None
    asyncio.run(main())


def test_handle_user_input_returns_immediately(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        # handle_request 对 user_input 应立即返回（不阻塞到 turn 结束）
        t0 = asyncio.get_event_loop().time()
        await proto.handle_request({"type": "user_input", "content": "go"})
        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed < 0.1  # 立即返回，而不是等 20*0.02=0.4s 的 turn
        proto.agent_loop.interrupt()  # 收尾
        if proto._turn_task:
            await asyncio.wait_for(proto._turn_task, timeout=2)
    asyncio.run(main())
