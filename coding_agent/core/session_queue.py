"""
提交/事件双队列会话层 - 参考 Codex 的 submission queue / event queue 架构

Codex 把 agent 驱动解耦成两条队列：调用方把 Op（UserInput / Interrupt / ...）
提交到 submission queue，一个 worker 消费并驱动模型 turn，产生的 Event 走 event
queue 流回。好处：提交不阻塞、turn 运行中可注入控制指令（中断）、事件可多订阅
（TUI + 日志 + 远程同时消费）。

我们不重写生成器主循环（AgentLoop.run 仍是"每轮引擎"，657 个测试依赖它），
而是在它外面套这一层：SessionQueue 把 run() 当引擎，加上提交队列 + 事件广播。
非侵入、可组合。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


# ── 提交操作（Op）──────────────────────────────────────────────────────
@dataclass
class UserInput:
    """一条用户输入，触发一个新 turn。"""
    text: str


@dataclass
class Interrupt:
    """中断当前正在运行的 turn（不关闭会话）。"""
    pass


@dataclass
class Shutdown:
    """关闭会话 worker（优雅停止）。"""
    pass


Op = UserInput | Interrupt | Shutdown


class SessionQueue:
    """
    在 AgentLoop 之上的提交/事件双队列。

    用法：
        sq = SessionQueue(agent_loop, state)
        sq.start()
        unsub = sq.subscribe(lambda ev: ...)   # 可多个订阅者
        await sq.submit(UserInput("hello"))
        ...
        await sq.submit(Interrupt())           # turn 运行中也能提交
        await sq.aclose()
    """

    def __init__(self, agent_loop: Any, state: Any):
        self._loop = agent_loop
        self._state = state
        self._submissions: asyncio.Queue[Op] = asyncio.Queue()
        self._subscribers: list[Any] = []
        self._worker: asyncio.Task | None = None
        self._running_turn = False

    # ── 订阅（事件多播）──
    def subscribe(self, cb: Any):
        """注册事件订阅者，返回取消订阅的函数。"""
        self._subscribers.append(cb)

        def _unsub():
            try:
                self._subscribers.remove(cb)
            except ValueError:
                pass
        return _unsub

    def _emit(self, event: Any) -> None:
        for cb in list(self._subscribers):
            try:
                cb(event)
            except Exception:
                pass  # 订阅者异常不影响主流程

    # ── 提交 ──
    async def submit(self, op: Op) -> None:
        """提交一个 Op。

        Interrupt 是控制信号，立即作用于正在运行的 turn（不排队等 UserInput
        消费完）；UserInput / Shutdown 走 FIFO 提交队列。
        """
        if isinstance(op, Interrupt):
            if self._running_turn:
                self._loop.interrupt()
            return
        await self._submissions.put(op)

    @property
    def is_running_turn(self) -> bool:
        return self._running_turn

    # ── worker ──
    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.ensure_future(self._run_worker())

    async def _run_worker(self) -> None:
        while True:
            op = await self._submissions.get()
            if isinstance(op, Shutdown):
                break
            if isinstance(op, UserInput):
                await self._drive_turn(op.text)

    async def _drive_turn(self, text: str) -> None:
        """驱动一个 turn：把 run() 生成器的事件广播给所有订阅者。"""
        self._running_turn = True
        try:
            async for event in self._loop.run(self._state, text):
                self._emit(event)
        finally:
            self._running_turn = False

    async def aclose(self) -> None:
        """优雅关闭 worker。"""
        await self._submissions.put(Shutdown())
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._worker, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker.cancel()
            self._worker = None
