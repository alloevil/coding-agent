"""
测试 file_edit / file_write 返回 unified diff（让模型看到实际改动）。
"""
import asyncio

from coding_agent.tools.file_ops import FileEditTool, FileWriteTool, _make_diff


def test_make_diff_basic():
    out = _make_diff("a\nb\nc\n", "a\nB\nc\n", "x.py")
    assert "```diff" in out
    assert "-b" in out and "+B" in out


def test_make_diff_no_change_empty():
    assert _make_diff("same\n", "same\n", "x.py") == ""


def test_make_diff_truncates():
    old = "\n".join(f"line{i}" for i in range(200))
    new = "\n".join(f"X{i}" for i in range(200))
    out = _make_diff(old, new, "x.py", max_lines=20)
    assert "truncated" in out


def test_edit_returns_diff(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")
    tool = FileEditTool()
    out = asyncio.run(tool.execute(path=str(f), old_text="y = 2", new_text="y = 3"))
    assert "Successfully edited" in out
    assert "```diff" in out
    assert "+y = 3" in out


def test_write_overwrite_shows_diff(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("old line\n", encoding="utf-8")
    tool = FileWriteTool()
    out = asyncio.run(tool.execute(path=str(f), content="new line\n"))
    assert "```diff" in out
    assert "-old line" in out and "+new line" in out


def test_write_new_file_no_diff(tmp_path):
    f = tmp_path / "fresh.txt"
    tool = FileWriteTool()
    out = asyncio.run(tool.execute(path=str(f), content="hello\n"))
    # 新建文件不回显 diff（避免整文件刷屏）
    assert "```diff" not in out
    assert "Successfully wrote" in out
