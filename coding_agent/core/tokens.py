"""
Token 计数 - 真 tokenizer（可选）+ 字符启发式兜底

之前各处用 len(text)//4 估算 token，对中文/代码/JSON 误差很大，导致压缩
触发点和预算停止不准。这里集中处理：

  - 若安装了 tiktoken（OpenAI 系 BPE），用真 tokenizer，按模型选编码；
    未知模型回退到 o200k_base（gpt-4o/gpt-5 系）。
  - 未安装 tiktoken 时回退到字符启发式（保持旧行为，不引入硬依赖）。

tiktoken 是可选依赖：装了就更准，没装也能跑。编码器按名缓存，只构建一次。
"""
from __future__ import annotations

from functools import lru_cache

# 字符兜底：平均每 token 约 4 字符（OpenAI 对英文文本的经验值）。
_CHARS_PER_TOKEN = 4


@lru_cache(maxsize=8)
def _get_encoder(model: str | None):
    """返回一个 tiktoken 编码器，或 None（未安装/不可用时）。按 model 缓存。"""
    try:
        import tiktoken
    except Exception:
        return None
    # 优先按模型名取编码；失败回退到通用编码（gpt-4o/gpt-5 用 o200k_base）。
    try:
        if model:
            return tiktoken.encoding_for_model(model)
    except Exception:
        pass
    for name in ("o200k_base", "cl100k_base"):
        try:
            return tiktoken.get_encoding(name)
        except Exception:
            continue
    return None


def count_tokens(text: str, model: str | None = None) -> int:
    """
    估算一段文本的 token 数。

    有 tiktoken 用真计数；否则用字符启发式 len//4。空串返回 0。
    """
    if not text:
        return 0
    enc = _get_encoder(model)
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return len(text) // _CHARS_PER_TOKEN


def using_real_tokenizer(model: str | None = None) -> bool:
    """当前是否在用真 tokenizer（供诊断/测试）。"""
    return _get_encoder(model) is not None
