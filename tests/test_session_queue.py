"""
测试提交/事件双队列会话层（core/session_queue.py）。

用一个假 AgentLoop（run 生成器 + interrupt）验证：提交输入收到事件、
多订阅者广播、turn 中途 Interrupt 生效、优雅关闭。
"""
import asyncio

import pytest

from coding_agent.core.session_queue import (
    SessionQueue, UserInput, Interrupt,
)


class _Ev:
    def __init__(self, name):
        self.event = name
        self.data = {}


class _FakeLoop:
    """假 AgentLoop：run() 逐个 yield 事件，可被 interrupt() 打断。"""
    def __init__(self, n_events=3, slow=0.0):
        self._interrupted = False
        self.n = n_events
        self.slow = slow
        self.interrupt_calls = 0

    def interrupt(self):
        self._interrupted = True
        self.interrupt_calls += 1

    async def run(self, state, text):
        self._interrupted = False
        for i in range(self.n):
            if self._interrupted:
                yield _Ev("interrupted")
                return
            if self.slow:
                await asyncio.sleep(self.slow)
            yield _Ev(f"event-{i}")
        yield _Ev("done")


def test_submit_input_receives_events():
    async def main():
        loop = _FakeLoop(n_events=2)
        sq = SessionQueue(loop, state=object())
        seen = []
        sq.subscribe(lambda ev: seen.append(ev.event))
        sq.start()
        await sq.submit(UserInput("hi"))
        # 等 turn 跑完
        for _ in range(50):
            if "done" in seen:
                break
            await asyncio.sleep(0.01)
        await sq.aclose()
        assert "event-0" in seen and "event-1" in seen and "done" in seen
    asyncio.run(main())


def test_multiple_subscribers():
    async def main():
        loop = _FakeLoop(n_events=1)
        sq = SessionQueue(loop, state=object())
        a, b = [], []
        sq.subscribe(lambda ev: a.append(ev.event))
        sq.subscribe(lambda ev: b.append(ev.event))
        sq.start()
        await sq.submit(UserInput("hi"))
        for _ in range(50):
            if "done" in a:
                break
            await asyncio.sleep(0.01)
        await sq.aclose()
        assert a == b and "done" in a  # 两个订阅者收到相同事件
    asyncio.run(main())


def test_interrupt_mid_turn():
    async def main():
        loop = _FakeLoop(n_events=10, slow=0.02)  # 慢 turn，给中断留时间
        sq = SessionQueue(loop, state=object())
        seen = []
        sq.subscribe(lambda ev: seen.append(ev.event))
        sq.start()
        await sq.submit(UserInput("go"))
        await asyncio.sleep(0.03)  # 让 turn 跑起来
        await sq.submit(Interrupt())  # 中途中断
        for _ in range(100):
            if "interrupted" in seen:
                break
            await asyncio.sleep(0.01)
        await sq.aclose()
        assert loop.interrupt_calls >= 1
        assert "interrupted" in seen
        assert "done" not in seen  # 没跑完
    asyncio.run(main())


def test_interrupt_when_idle_is_noop():
    async def main():
        loop = _FakeLoop(n_events=1)
        sq = SessionQueue(loop, state=object())
        sq.start()
        await sq.submit(Interrupt())  # 没有 turn 在跑
        await asyncio.sleep(0.02)
        await sq.aclose()
        assert loop.interrupt_calls == 0  # 空闲时中断被忽略
    asyncio.run(main())


def test_unsubscribe():
    async def main():
        loop = _FakeLoop(n_events=1)
        sq = SessionQueue(loop, state=object())
        seen = []
        unsub = sq.subscribe(lambda ev: seen.append(ev.event))
        unsub()  # 立即退订
        sq.start()
        await sq.submit(UserInput("hi"))
        await asyncio.sleep(0.05)
        await sq.aclose()
        assert seen == []  # 退订后收不到
    asyncio.run(main())


def test_sequential_turns():
    async def main():
        loop = _FakeLoop(n_events=1)
        sq = SessionQueue(loop, state=object())
        seen = []
        sq.subscribe(lambda ev: seen.append(ev.event))
        sq.start()
        await sq.submit(UserInput("first"))
        await sq.submit(UserInput("second"))
        for _ in range(100):
            if seen.count("done") >= 2:
                break
            await asyncio.sleep(0.01)
        await sq.aclose()
        assert seen.count("done") == 2  # 两个 turn 都跑了
    asyncio.run(main())
