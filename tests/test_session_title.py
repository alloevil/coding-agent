"""
测试会话标题生成：启发式 + 模型（带桩）+ SessionStore.set_title。
"""
import asyncio

from coding_agent.core.state import AgentState
from coding_agent.core.session_title import (
    heuristic_title,
    generate_title,
    _truncate,
    MAX_TITLE_LEN,
)
from coding_agent.memory.session import SessionStore


def _state_with_user(text):
    s = AgentState(session_id="t")
    s.add_user_message(text)
    return s


def test_heuristic_from_first_user_message():
    s = _state_with_user("Fix the login bug in auth.py")
    assert heuristic_title(s) == "Fix the login bug in auth.py"


def test_heuristic_strips_slash_command():
    s = _state_with_user("/fix the broken parser")
    assert heuristic_title(s) == "the broken parser"


def test_heuristic_collapses_whitespace_and_first_line():
    s = _state_with_user("Add   feature\nthen do other stuff")
    assert heuristic_title(s) == "Add feature"


def test_heuristic_empty():
    s = AgentState(session_id="t")
    assert heuristic_title(s) == "Untitled session"


def test_truncate_word_boundary():
    long = "word " * 40
    out = _truncate(long.strip())
    assert len(out) <= MAX_TITLE_LEN + 1  # +1 for the ellipsis
    assert out.endswith("…")


def test_generate_title_uses_model():
    s = _state_with_user("Refactor the data pipeline into stages")

    async def fake_model(messages, tools):
        assert tools == []  # 单轮、无工具
        return {"content": "Refactor data pipeline"}

    title = asyncio.run(generate_title(s, fake_model))
    assert title == "Refactor data pipeline"


def test_generate_title_strips_quotes_and_period():
    s = _state_with_user("do a thing")

    async def fake_model(messages, tools):
        return {"content": '"Do A Thing."'}

    assert asyncio.run(generate_title(s, fake_model)) == "Do A Thing"


def test_generate_title_falls_back_on_error():
    s = _state_with_user("Fix the login bug")

    async def boom(messages, tools):
        raise RuntimeError("model down")

    assert asyncio.run(generate_title(s, boom)) == "Fix the login bug"


def test_generate_title_falls_back_on_empty():
    s = _state_with_user("Fix the login bug")

    async def empty(messages, tools):
        return {"content": ""}

    assert asyncio.run(generate_title(s, empty)) == "Fix the login bug"


def test_generate_title_no_model():
    s = _state_with_user("Fix the login bug")
    assert asyncio.run(generate_title(s, None)) == "Fix the login bug"


def test_session_store_set_title(tmp_path):
    store = SessionStore(str(tmp_path / "s.db"))
    sid = store.create_session()
    store.set_title(sid, "My Title")
    sessions = store.list_sessions()
    match = [x for x in sessions if x["id"] == sid][0]
    assert match["metadata"]["title"] == "My Title"


def test_session_store_set_title_unknown_id_noop(tmp_path):
    store = SessionStore(str(tmp_path / "s.db"))
    # 不应抛出
    store.set_title("does-not-exist", "x")
