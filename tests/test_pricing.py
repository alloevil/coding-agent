"""
测试 /cost 美元计价（core/pricing.py + commands._cmd_cost 接线）。
"""
from coding_agent.core.pricing import lookup_price, estimate_cost
from coding_agent.core.commands import dispatch, CommandContext


def _ctx(**kw):
    base = dict(tool_names=[], total_prompt_tokens=1_000_000,
                total_completion_tokens=100_000, model="claude-opus-4-8")
    base.update(kw)
    return CommandContext(**base)


def test_longest_prefix_match():
    # claude-opus-4-8 命中 claude-opus-4，不是更短的别的前缀
    assert lookup_price("claude-opus-4-8") == (15.0, 75.0)
    # gpt-5-mini 命中 gpt-5-mini（更长前缀），不是 gpt-5
    assert lookup_price("gpt-5-mini") == (0.25, 2.0)
    assert lookup_price("gpt-5") == (1.25, 10.0)


def test_unknown_model_is_none():
    assert lookup_price("mimo-v2.5-pro") is None
    assert estimate_cost("mimo-v2.5-pro", 1000, 1000) is None


def test_override_wins():
    assert lookup_price("mimo-v2.5-pro",
                        override={"input": 1.0, "output": 2.0}) == (1.0, 2.0)
    # 覆盖也适用于已知模型（网关价 ≠ 牌价）
    assert lookup_price("claude-opus-4-8",
                        override={"input": 0.5, "output": 1.0}) == (0.5, 1.0)


def test_bad_override_falls_back_to_table():
    assert lookup_price("claude-opus-4-8",
                        override={"input": "not-a-number", "output": 1}) == (15.0, 75.0)


def test_estimate_math():
    # 1M in @ $15 + 0.1M out @ $75 = 15 + 7.5 = 22.5
    est = estimate_cost("claude-opus-4-8", 1_000_000, 100_000)
    assert est is not None and abs(est - 22.5) < 1e-9


def test_cost_command_shows_dollars_for_known_model():
    res = dispatch("/cost", _ctx())
    assert res.kind == "print"
    assert "$22.5000" in res.payload


def test_cost_command_omits_dollars_for_unknown_model():
    res = dispatch("/cost", _ctx(model="mimo-v2.5-pro"))
    assert "$" not in res.payload  # 不瞎猜


def test_cost_command_uses_config_pricing_override():
    res = dispatch("/cost", _ctx(model="mimo-v2.5-pro",
                                 pricing={"input": 1.0, "output": 1.0}))
    # 1M*1 + 0.1M*1 = 1.1
    assert "$1.1000" in res.payload
