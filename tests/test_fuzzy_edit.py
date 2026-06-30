"""
测试多策略模糊替换 fuzzy_replace（opencode 风格 edit 级联）
"""
import pytest

from coding_agent.core.text_replace import fuzzy_replace, ReplaceError


def test_exact_match():
    assert fuzzy_replace("a = 1\nb = 2\n", "b = 2", "b = 3") == "a = 1\nb = 3\n"


def test_line_trimmed_tolerates_trailing_whitespace():
    # 文件里行尾有多余空格，old_text 没有 —— 精确匹配失败，行级 strip 命中
    content = "def f():   \n    return 1\n"
    out = fuzzy_replace(content, "def f():\n    return 1", "def f():\n    return 2")
    assert "return 2" in out


def test_whitespace_normalized_tolerates_internal_spacing():
    # 内部空白量不同（多个空格 vs 单空格）
    content = "x  =    1\n"
    out = fuzzy_replace(content, "x = 1", "x = 2")
    assert out == "x = 2\n"


def test_indentation_flexible():
    # old_text 用 0 缩进，文件里整体缩进了 8 空格
    content = "        if cond:\n            do()\n"
    out = fuzzy_replace(content, "if cond:\n    do()", "if cond:\n    done()")
    assert "done()" in out


def test_block_anchor_matches_by_first_last_line():
    content = "\n".join([
        "def big():",
        "    a = 1",
        "    b = 2  # drifted comment",
        "    return a + b",
    ]) + "\n"
    # old_text 中间行细节不同，但首尾行作锚点能匹配
    old = "def big():\n    a = 999\n    return a + b"
    out = fuzzy_replace(content, old, "def big():\n    return 0")
    assert "return 0" in out


def test_not_found_raises():
    with pytest.raises(ReplaceError) as ei:
        fuzzy_replace("abc\n", "xyz", "q")
    assert "not found" in str(ei.value)


def test_multiple_matches_raises():
    with pytest.raises(ReplaceError) as ei:
        fuzzy_replace("a = 1\na = 1\n", "a = 1", "a = 2")
    assert "multiple matches" in str(ei.value)


def test_replace_all():
    assert fuzzy_replace("a = 1\na = 1\n", "a = 1", "a = 2", replace_all=True) == "a = 2\na = 2\n"


def test_identical_raises():
    with pytest.raises(ReplaceError):
        fuzzy_replace("x\n", "x", "x")


def test_disproportionate_match_rejected():
    # old_text 一行，但唯一可匹配的块跨很多行 —— 应拒绝
    content = "start\n" + "\n".join(f"line{i}" for i in range(20)) + "\nend\n"
    # 故意用首尾行作 old，使 block_anchor 想匹配整个 20 行块
    with pytest.raises(ReplaceError):
        fuzzy_replace(content, "start\nXXX\nend", "replaced")


def test_escape_normalized():
    # 文件里是真实换行；old_text 写成了字面 \n
    content = "line1\nline2\n"
    out = fuzzy_replace(content, "line1\\nline2", "replaced")
    assert "replaced" in out


def test_trimmed_boundary():
    # old_text 带多余前后空白
    content = "alpha\nbeta\n"
    out = fuzzy_replace(content, "  beta  ", "BETA")
    assert "BETA" in out
