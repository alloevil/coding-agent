"""
测试 ask_user 工具
"""
import pytest

from coding_agent.tools.ask_ops import AskUserTool, register_ask_tools
from coding_agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_ask_returns_answer_via_handler():
    async def handler(q, opts):
        assert q == "Which DB?"
        assert opts == ["postgres", "sqlite"]
        return "sqlite"
    t = AskUserTool(handler=handler)
    out = await t.execute(question="Which DB?", options=["postgres", "sqlite"])
    assert out == "User answered: sqlite"


@pytest.mark.asyncio
async def test_ask_no_handler_degrades():
    t = AskUserTool(handler=None)
    out = await t.execute(question="anything?")
    assert "No interactive user" in out


@pytest.mark.asyncio
async def test_ask_requires_question():
    t = AskUserTool(handler=None)
    out = await t.execute(question="")
    assert out.startswith("Error")


@pytest.mark.asyncio
async def test_handler_exception_handled():
    async def boom(q, opts):
        raise RuntimeError("nope")
    t = AskUserTool(handler=boom)
    out = await t.execute(question="q")
    assert out.startswith("Error obtaining user answer")


def test_register_and_permission():
    reg = ToolRegistry()
    t = register_ask_tools(registry=reg)
    assert reg.get_tool("ask_user") is t
    assert t.permission.value == "read"


@pytest.mark.asyncio
async def test_set_handler_late():
    t = AskUserTool()
    async def h(q, opts): return "yes"
    t.set_handler(h)
    out = await t.execute(question="ok?")
    assert out == "User answered: yes"
