"""
测试 apply_patch 多文件原子补丁工具
"""
import pytest

from coding_agent.tools.patch_ops import (
    parse_patch,
    apply_patch,
    PatchError,
    ApplyPatchTool,
)


def _patch(*body_lines):
    return "\n".join(["*** Begin Patch", *body_lines, "*** End Patch"])


def test_add_file(tmp_path):
    p = _patch("*** Add File: a.txt", "+hello", "+world")
    out = apply_patch(p, root=tmp_path)
    assert (tmp_path / "a.txt").read_text() == "hello\nworld"
    assert "added" in out


def test_update_file_replaces_block(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("def f():\n    return 1\n")
    p = _patch(
        "*** Update File: code.py",
        "@@",
        "-    return 1",
        "+    return 2",
    )
    apply_patch(p, root=tmp_path)
    assert "return 2" in target.read_text()
    assert "return 1" not in target.read_text()


def test_delete_file(tmp_path):
    target = tmp_path / "gone.txt"
    target.write_text("bye")
    p = _patch("*** Delete File: gone.txt")
    apply_patch(p, root=tmp_path)
    assert not target.exists()


def test_multi_file_atomic(tmp_path):
    (tmp_path / "x.py").write_text("a = 1\n")
    p = _patch(
        "*** Add File: new.py",
        "+created",
        "*** Update File: x.py",
        "@@",
        "-a = 1",
        "+a = 2",
    )
    apply_patch(p, root=tmp_path)
    assert (tmp_path / "new.py").read_text() == "created"
    assert "a = 2" in (tmp_path / "x.py").read_text()


def test_atomicity_rollback_on_failure(tmp_path):
    """若其中一个 hunk 校验失败，任何文件都不应被改动。"""
    (tmp_path / "x.py").write_text("a = 1\n")
    p = _patch(
        "*** Add File: new.py",
        "+should not persist",
        "*** Update File: x.py",
        "@@",
        "-nonexistent line",
        "+whatever",
    )
    with pytest.raises(PatchError):
        apply_patch(p, root=tmp_path)
    # new.py 不应被创建（校验阶段就失败）
    assert not (tmp_path / "new.py").exists()
    # x.py 保持原样
    assert (tmp_path / "x.py").read_text() == "a = 1\n"


def test_ambiguous_context_errors(tmp_path):
    (tmp_path / "x.py").write_text("dup\ndup\n")
    p = _patch("*** Update File: x.py", "@@", "-dup", "+changed")
    with pytest.raises(PatchError) as ei:
        apply_patch(p, root=tmp_path)
    assert "ambiguous" in str(ei.value)


def test_add_existing_file_errors(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    p = _patch("*** Add File: a.txt", "+y")
    with pytest.raises(PatchError):
        apply_patch(p, root=tmp_path)


def test_parse_requires_envelope():
    with pytest.raises(PatchError):
        parse_patch("*** Add File: a.txt\n+x")


def test_context_line_preserved(tmp_path):
    target = tmp_path / "c.py"
    target.write_text("line1\nline2\nline3\n")
    p = _patch(
        "*** Update File: c.py",
        "@@",
        " line1",
        "-line2",
        "+line2_new",
        " line3",
    )
    apply_patch(p, root=tmp_path)
    assert target.read_text() == "line1\nline2_new\nline3\n"


@pytest.mark.asyncio
async def test_tool_returns_error_string_not_raise(tmp_path):
    tool = ApplyPatchTool()
    out = await tool.execute(patch="garbage", root=str(tmp_path))
    assert out.startswith("Error applying patch")
