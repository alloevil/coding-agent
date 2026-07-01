"""
端到端 agent 循环测试：用可编排的 mock 模型驱动 AgentLoop.run()，验证
「模型 → 工具执行 → 结果回填 → 再调模型 → 完成」的完整编排，而不是单个工具。

不打真实网络：set_model_call_fn 注入一个按脚本返回的假模型。
"""
import json

import pytest

from coding_agent.core import AgentState, AgentConfig, AgentLoop, AgentEvent
from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.file_ops import register_file_tools
from coding_agent.tools.shell import register_shell_tools


def _tool_call(name: str, args: dict, cid: str = "c1") -> dict:
    """构造一个 OpenAI 形态的 tool_call。"""
    return {"id": cid, "function": {"name": name, "arguments": json.dumps(args)}}


def scripted_model(*responses):
    """返回一个 model_call_fn：每次调用弹出下一条预设响应；用尽后返回纯文本收尾。"""
    seq = list(responses)

    async def _fn(context, tools):
        if seq:
            return seq.pop(0)
        return {"content": "done", "tool_calls": []}

    return _fn


@pytest.fixture
def registry():
    reg = ToolRegistry()
    register_file_tools(reg)
    register_shell_tools(reg)
    return reg


@pytest.fixture
def config():
    return AgentConfig(model="mock", api_key="k", max_turns=10, auto_approve=True)


async def _collect(loop, state, user_input):
    return [ev async for ev in loop.run(state, user_input)]


async def test_plain_text_response_ends_turn(config, registry):
    """模型只回文本、无工具 → 一轮 ASSISTANT_MESSAGE + DONE。"""
    loop = AgentLoop(config=config, tool_registry=registry, register_builtin_tools=False)
    loop.set_model_call_fn(scripted_model({"content": "hello", "tool_calls": []}))
    events = await _collect(loop, AgentState(), "hi")
    kinds = [e.event for e in events]
    assert AgentEvent.ASSISTANT_MESSAGE in kinds
    assert AgentEvent.DONE in kinds


async def test_tool_call_then_finish(config, registry, tmp_path):
    """模型先调 shell_exec，拿到结果后再回文本收尾 → 覆盖「工具→回填→再调模型」。"""
    loop = AgentLoop(config=config, tool_registry=registry, register_builtin_tools=False)
    loop.set_model_call_fn(scripted_model(
        {"content": "", "tool_calls": [_tool_call("shell_exec", {"command": "echo hi"})]},
        {"content": "finished", "tool_calls": []},
    ))
    events = await _collect(loop, AgentState(), "run echo")
    kinds = [e.event for e in events]
    assert AgentEvent.TOOL_CALL in kinds
    assert AgentEvent.TOOL_RESULT in kinds
    assert AgentEvent.DONE in kinds
    # 工具结果应回填进消息历史（供下一轮模型看到）
    # 至少经历了 2 次模型调用（否则不会收到 finished）
    assert any("finished" in (e.data.get("content", "") or "")
               for e in events if e.event == AgentEvent.ASSISTANT_MESSAGE)


async def test_tool_result_written_to_state(config, registry, tmp_path):
    """工具真的执行了副作用：写文件工具落盘。"""
    loop = AgentLoop(config=config, tool_registry=registry, register_builtin_tools=False)
    target = tmp_path / "out.txt"
    loop.set_model_call_fn(scripted_model(
        {"content": "", "tool_calls": [
            _tool_call("file_write", {"path": str(target), "content": "payload"})]},
        {"content": "wrote it", "tool_calls": []},
    ))
    await _collect(loop, AgentState(), "write file")
    assert target.read_text() == "payload"


async def test_multi_round_tool_calls(config, registry, tmp_path):
    """连续两轮工具调用，验证多轮编排不串味。"""
    loop = AgentLoop(config=config, tool_registry=registry, register_builtin_tools=False)
    f1, f2 = tmp_path / "a.txt", tmp_path / "b.txt"
    loop.set_model_call_fn(scripted_model(
        {"content": "", "tool_calls": [_tool_call("file_write", {"path": str(f1), "content": "1"})]},
        {"content": "", "tool_calls": [_tool_call("file_write", {"path": str(f2), "content": "2"})]},
        {"content": "both done", "tool_calls": []},
    ))
    await _collect(loop, AgentState(), "write two")
    assert f1.read_text() == "1"
    assert f2.read_text() == "2"


async def test_max_turns_stops_loop(registry):
    """模型永远回工具调用 → max_turns 兜底停止，不会无限循环。"""
    cfg = AgentConfig(model="mock", api_key="k", max_turns=3, auto_approve=True)
    loop = AgentLoop(config=cfg, tool_registry=registry, register_builtin_tools=False)

    async def always_tool(context, tools):
        return {"content": "", "tool_calls": [_tool_call("shell_exec", {"command": "true"})]}

    loop.set_model_call_fn(always_tool)
    state = AgentState()
    events = await _collect(loop, state, "loop forever")
    # 轮数受 max_turns 限制
    assert state.turn_count <= 3
    # 结束了（生成器耗尽）
    assert len(events) > 0


async def test_model_exception_yields_error_and_stops(config, registry):
    """模型抛异常 → ERROR 事件 + 停止，不崩溃。"""
    loop = AgentLoop(config=config, tool_registry=registry, register_builtin_tools=False)

    async def boom(context, tools):
        raise RuntimeError("model blew up")

    loop.set_model_call_fn(boom)
    events = await _collect(loop, AgentState(), "hi")
    err = [e for e in events if e.event == AgentEvent.ERROR]
    assert err and "blew up" in err[0].data.get("error", "")


async def test_token_budget_stops_before_call(registry):
    """超出 token 预算 → DONE(reason=token_budget_exceeded)，在调模型前就停。"""
    cfg = AgentConfig(model="mock", api_key="k", max_turns=10,
                      auto_approve=True, max_total_tokens=100)
    loop = AgentLoop(config=cfg, tool_registry=registry, register_builtin_tools=False)
    loop.set_token_usage_fn(lambda: 999)  # 已超预算
    called = {"n": 0}

    async def counting(context, tools):
        called["n"] += 1
        return {"content": "x", "tool_calls": []}

    loop.set_model_call_fn(counting)
    events = await _collect(loop, AgentState(), "hi")
    done = [e for e in events if e.event == AgentEvent.DONE]
    assert done and done[0].data.get("reason") == "token_budget_exceeded"
    assert called["n"] == 0  # 预算已超，根本没调模型
