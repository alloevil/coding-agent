"""
模型计价 — /cost 的美元估算（Claude Code parity）。

单价表按「每 1M token 美元」记（公开牌价，2026 年中；直连官方 API 时准确，
网关/转售价不一定一致——所以 config.pricing 可覆盖，未知模型则不显示美元，
不瞎猜）。

用法：
    est = estimate_cost(model, prompt_tokens, completion_tokens, override=config.pricing)
    if est is not None: print(f"≈ ${est:.4f}")
"""
from __future__ import annotations

from typing import Any

# (前缀, input $/1M, output $/1M)。按最长前缀匹配；顺序无关。
# 只收录常见且牌价稳定的；其余靠 config.pricing 覆盖。
PRICES: list[tuple[str, float, float]] = [
    # Anthropic
    ("claude-opus-4", 15.0, 75.0),
    ("claude-sonnet-4", 3.0, 15.0),
    ("claude-sonnet-5", 3.0, 15.0),
    ("claude-haiku-4", 0.80, 4.0),
    ("claude-fable-5", 5.0, 25.0),
    # OpenAI
    ("gpt-5-mini", 0.25, 2.0),
    ("gpt-5", 1.25, 10.0),
    ("gpt-4o-mini", 0.15, 0.60),
    ("gpt-4o", 2.50, 10.0),
    ("gpt-4.1-mini", 0.40, 1.60),
    ("gpt-4.1", 2.0, 8.0),
    # DeepSeek
    ("deepseek-chat", 0.27, 1.10),
    ("deepseek-reasoner", 0.55, 2.19),
]


def lookup_price(model: str,
                 override: dict[str, Any] | None = None) -> tuple[float, float] | None:
    """
    返回 (input $/1M, output $/1M)；未知模型且无覆盖时 None。

    override（来自 config.pricing）优先：{"input": 3.0, "output": 15.0}。
    内置表按最长前缀匹配（"claude-opus-4-8" 命中 "claude-opus-4"）。
    """
    if override and "input" in override and "output" in override:
        try:
            return float(override["input"]), float(override["output"])
        except (TypeError, ValueError):
            pass
    m = (model or "").lower()
    best: tuple[str, float, float] | None = None
    for prefix, pin, pout in PRICES:
        if m.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, pin, pout)
    return (best[1], best[2]) if best else None


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int,
                  override: dict[str, Any] | None = None) -> float | None:
    """美元成本估算；未知模型返回 None（调用方就不显示美元）。"""
    price = lookup_price(model, override)
    if price is None:
        return None
    pin, pout = price
    return (prompt_tokens * pin + completion_tokens * pout) / 1_000_000
