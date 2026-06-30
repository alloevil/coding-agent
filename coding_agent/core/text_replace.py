"""
多策略文本替换 - 让 file_edit 对空白/缩进漂移更鲁棒

设计参考开源项目 opencode（sst/opencode, MIT）的 edit replacer 级联思路：
按从严到宽的顺序尝试一组策略，每个策略产出"候选匹配子串"，取第一个在
内容中**唯一存在**的候选完成替换；replace_all 时替换全部。

这是独立的 Python 重新实现（非源码移植），核心策略：
  1. Exact          —— 精确匹配（最严格）
  2. LineTrimmed    —— 逐行 strip 后比较（容忍行首尾空白差异）
  3. WhitespaceNorm —— 把连续空白折叠成单空格后比较（容忍空白量差异）
  4. IndentFlexible —— 去掉公共缩进后比较（容忍整体缩进层级差异）
  5. BlockAnchor    —— 用首尾行作锚点匹配整块（容忍中间内容细节）

每个候选还要过"比例守卫"：匹配跨度远大于 old_text 时拒绝，避免误伤。
"""
from __future__ import annotations

from typing import Iterator


class ReplaceError(Exception):
    """替换失败（未找到 / 多处匹配 / 比例失衡）。"""


def _exact(content: str, find: str) -> Iterator[str]:
    yield find


def _line_trimmed(content: str, find: str) -> Iterator[str]:
    """逐行 strip 后相等即匹配，产出原文中对应的精确子串。"""
    olines = content.split("\n")
    slines = find.split("\n")
    if slines and slines[-1] == "":
        slines.pop()
    if not slines:
        return
    for i in range(0, len(olines) - len(slines) + 1):
        if all(olines[i + j].strip() == slines[j].strip() for j in range(len(slines))):
            start = sum(len(olines[k]) + 1 for k in range(i))
            end = start
            for k in range(len(slines)):
                end += len(olines[i + k])
                if k < len(slines) - 1:
                    end += 1
            yield content[start:end]


def _whitespace_normalized(content: str, find: str) -> Iterator[str]:
    """把连续空白折叠成单空格后比较（行级与块级）。"""
    import re
    norm = lambda t: re.sub(r"\s+", " ", t).strip()
    nfind = norm(find)
    lines = content.split("\n")
    for line in lines:
        if norm(line) == nfind:
            yield line
    flines = find.split("\n")
    if len(flines) > 1:
        for i in range(0, len(lines) - len(flines) + 1):
            block = "\n".join(lines[i:i + len(flines)])
            if norm(block) == nfind:
                yield block


def _indentation_flexible(content: str, find: str) -> Iterator[str]:
    """去掉公共最小缩进后比较整块。"""
    def dedent(text: str) -> str:
        lines = text.split("\n")
        nonempty = [l for l in lines if l.strip()]
        if not nonempty:
            return text
        min_indent = min(len(l) - len(l.lstrip()) for l in nonempty)
        return "\n".join(l if not l.strip() else l[min_indent:] for l in lines)

    nfind = dedent(find)
    clines = content.split("\n")
    flines = find.split("\n")
    for i in range(0, len(clines) - len(flines) + 1):
        block = "\n".join(clines[i:i + len(flines)])
        if dedent(block) == nfind:
            yield block


def _block_anchor(content: str, find: str) -> Iterator[str]:
    """用首/尾行作锚点匹配整块（块需 >=3 行）。"""
    olines = content.split("\n")
    slines = find.split("\n")
    if slines and slines[-1] == "":
        slines.pop()
    if len(slines) < 3:
        return
    first = slines[0].strip()
    last = slines[-1].strip()
    size = len(slines)
    max_delta = max(1, size // 4)
    for i in range(len(olines)):
        if olines[i].strip() != first:
            continue
        for j in range(i + 2, len(olines)):
            if olines[j].strip() == last:
                if abs((j - i + 1) - size) <= max_delta:
                    yield "\n".join(olines[i:j + 1])
                break


def _escape_normalized(content: str, find: str) -> Iterator[str]:
    """把 find 里的字面转义（\\n \\t \\" 等）解为真实字符后再按行级匹配。

    模型有时把 old_text 写成带字面反斜杠的形式（例如从字符串字面量里拷的）。
    """
    import re

    def unescape(s: str) -> str:
        return re.sub(
            r'\\(n|t|r|\'|"|`|\\)',
            lambda m: {"n": "\n", "t": "\t", "r": "\r",
                       "'": "'", '"': '"', "`": "`", "\\": "\\"}[m.group(1)],
            s,
        )

    nfind = unescape(find)
    if nfind == find:
        return  # 没有可解的转义，交给其它策略
    # 解码后做精确 + 行级 strip 两种尝试
    if nfind in content:
        yield nfind
    yield from _line_trimmed(content, nfind)


def _trimmed_boundary(content: str, find: str) -> Iterator[str]:
    """忽略 find 首尾整体空白后匹配（容忍多余的前导/尾随空行或空格）。"""
    stripped = find.strip()
    if stripped and stripped != find and stripped in content:
        yield stripped


_STRATEGIES = [
    _exact,
    _line_trimmed,
    _whitespace_normalized,
    _indentation_flexible,
    _block_anchor,
    _escape_normalized,
    _trimmed_boundary,
]


def _disproportionate(search: str, old: str) -> bool:
    """匹配跨度相对 old_text 失衡时返回 True（拒绝，避免误伤大段）。"""
    old_lines = old.count("\n") + 1
    search_lines = search.count("\n") + 1
    if search_lines >= max(old_lines + 3, old_lines * 2):
        return True
    if old_lines == 1:
        return False
    return len(search.strip()) > max(len(old.strip()) + 500, len(old.strip()) * 4)


def fuzzy_replace(content: str, old: str, new: str, replace_all: bool = False) -> str:
    """
    多策略替换。返回新内容。

    Raises:
        ReplaceError: 未找到 / 多处匹配 / 比例失衡 / old==new / old 为空
    """
    if old == new:
        raise ReplaceError("old_text and new_text are identical; nothing to do")
    if old == "":
        raise ReplaceError("old_text cannot be empty")

    found_any = False
    for strategy in _STRATEGIES:
        for search in strategy(content, old):
            idx = content.find(search)
            if idx == -1:
                continue
            found_any = True
            if _disproportionate(search, old):
                raise ReplaceError(
                    "matched span is much larger than old_text; provide the exact "
                    "old_text for the intended replacement"
                )
            if replace_all:
                return content.replace(search, new)
            if idx != content.rfind(search):
                # 非唯一，换下一个候选/策略
                continue
            return content[:idx] + new + content[idx + len(search):]

    if not found_any:
        raise ReplaceError(
            "old_text not found (must match exactly, including whitespace and indentation)"
        )
    raise ReplaceError(
        "found multiple matches for old_text; add more surrounding context to make it unique"
    )
