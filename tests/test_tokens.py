"""
测试 token 计数：tiktoken 优先 + 字符兜底。
"""
from coding_agent.core import tokens
from coding_agent.core.tokens import count_tokens, using_real_tokenizer
from coding_agent.core.state import AgentState


def test_empty_is_zero():
    assert count_tokens("") == 0
    assert count_tokens(None or "") == 0


def test_count_positive():
    assert count_tokens("hello world this is a test") > 0


def test_fallback_when_no_tiktoken(monkeypatch):
    # 强制走兜底：让 _get_encoder 返回 None
    tokens._get_encoder.cache_clear()
    monkeypatch.setattr(tokens, "_get_encoder", lambda model=None: None)
    # 24 字符 // 4 = 6
    assert count_tokens("a" * 24) == 6


def test_real_tokenizer_used_when_available(monkeypatch):
    class _FakeEnc:
        def encode(self, text):
            return text.split()  # 每个词 1 token
    tokens._get_encoder.cache_clear()
    monkeypatch.setattr(tokens, "_get_encoder", lambda model=None: _FakeEnc())
    assert count_tokens("one two three") == 3
    assert using_real_tokenizer() is True


def test_state_estimate_uses_tokens(monkeypatch):
    class _FakeEnc:
        def encode(self, text):
            return list(text)  # 每字符 1 token，便于断言
    tokens._get_encoder.cache_clear()
    monkeypatch.setattr(tokens, "_get_encoder", lambda model=None: _FakeEnc())
    s = AgentState(session_id="t")
    s.metadata["model"] = "gpt-5-mini"
    s.add_user_message("hello")  # 5 字符 → 5 token
    assert s.get_token_estimate() == 5


def test_state_estimate_fallback(monkeypatch):
    tokens._get_encoder.cache_clear()
    monkeypatch.setattr(tokens, "_get_encoder", lambda model=None: None)
    s = AgentState(session_id="t")
    s.add_user_message("a" * 40)  # 40 // 4 = 10
    assert s.get_token_estimate() == 10
