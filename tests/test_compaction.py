"""
测试 context 压缩 (ContextManager.compact 及其各层)

重点验证生产关键不变量：
- 压缩后不会留下孤立的 tool 结果消息（会被 OpenAI 兼容 API 拒绝）
- auto-compact 保留最近若干轮原文，旧历史变成一条 summary
- token 估算计入工具结果
"""
import pytest

from coding_agent.context.manager import ContextManager
from coding_agent.core.state import AgentState, Message, MessageRole, ToolCall, ToolResult


def _user(text):
    return Message(role=MessageRole.USER, content=text)


def _assistant(text, tool_calls=None):
    return Message(role=MessageRole.ASSISTANT, content=text, tool_calls=tool_calls)


def _tool(call_id, content):
    return Message(role=MessageRole.TOOL,
                   tool_result=ToolResult(tool_call_id=call_id, content=content))


def _mk_state(messages):
    s = AgentState()
    s.messages = list(messages)
    return s


def test_token_estimate_counts_tool_results():
    # 用多样化的真实文本，避免重复字符在真 tokenizer 下被极端压缩
    # （"XXXX..." 在 BPE 下会被合并成极少 token，是测试假象而非真实负载）。
    blob = ("def process(item):\n    return item.value * 2  # line\n") * 100
    state = _mk_state([_tool("c1", blob)])
    empty = _mk_state([]).get_token_estimate()
    # 工具结果应显著贡献 token（对真 tokenizer 与字符兜底都成立）
    assert state.get_token_estimate() > empty + 200


def test_snip_truncates_long_tool_results_head_and_tail():
    cm = ContextManager(max_tokens=100)
    long = "HEAD" + "m" * 20000 + "TAIL"
    state = _mk_state([_tool("c1", long)])
    cm._try_snip(state)
    content = state.messages[0].tool_result.content
    assert content.startswith("HEAD")
    assert content.endswith("TAIL")
    assert "truncated" in content
    assert len(content) < len(long)


def test_budget_reduction_never_leaves_orphan_tool_result():
    # 构造：每个 assistant tool_calls 后跟一个 tool 结果
    msgs = []
    for i in range(20):
        msgs.append(_assistant("", [ToolCall(id=f"c{i}", name="shell_exec",
                                              arguments={"command": "x" * 200})]))
        msgs.append(_tool(f"c{i}", "y" * 2000))
    state = _mk_state(msgs)

    cm = ContextManager(max_tokens=1000)  # 强制大幅截断
    cm._try_budget_reduction(state)

    # 保留窗口第一条不能是 tool 结果
    assert state.messages, "should keep some messages"
    assert state.messages[0].role != MessageRole.TOOL


@pytest.mark.asyncio
async def test_auto_compact_keeps_recent_and_summarizes_old():
    # 30 条消息，应保留最近 ~12 条，其余总结为一条 system
    msgs = [_user(f"msg {i}") for i in range(30)]
    state = _mk_state(msgs)

    async def fake_model(context, tools):
        # 校验真实签名 (context, tools) 被正确调用
        assert isinstance(context, list)
        assert tools == []
        return {"content": "SUMMARY_OF_OLD", "tool_calls": []}

    cm = ContextManager(max_tokens=10000)
    await cm._auto_compact(state, fake_model)

    # 第一条是 summary（system）
    assert state.messages[0].role == MessageRole.SYSTEM
    assert "SUMMARY_OF_OLD" in state.messages[0].content
    # 最近的原文消息仍在
    assert any(m.content == "msg 29" for m in state.messages)
    assert any(m.content == "msg 28" for m in state.messages)
    # 旧消息已被丢弃
    assert not any(m.content == "msg 0" for m in state.messages)


@pytest.mark.asyncio
async def test_auto_compact_recent_window_not_orphaned():
    # 保留窗口边界恰好落在 tool 结果上时，应向后调整
    msgs = []
    for i in range(20):
        msgs.append(_assistant("", [ToolCall(id=f"c{i}", name="t", arguments={})]))
        msgs.append(_tool(f"c{i}", f"result {i}"))
    state = _mk_state(msgs)

    async def fake_model(context, tools):
        return {"content": "S", "tool_calls": []}

    cm = ContextManager(max_tokens=10000)
    await cm._auto_compact(state, fake_model)

    # summary 之后第一条不能是孤立 tool 结果
    assert state.messages[0].role == MessageRole.SYSTEM
    assert state.messages[1].role != MessageRole.TOOL


@pytest.mark.asyncio
async def test_compact_falls_back_to_budget_when_no_model():
    msgs = [_user("x" * 8000) for _ in range(10)]
    state = _mk_state(msgs)
    before = len(state.messages)

    cm = ContextManager(max_tokens=1000)
    await cm.compact(state, model_call_fn=None)

    # 没有模型时应通过硬截断减少消息数
    assert len(state.messages) < before
