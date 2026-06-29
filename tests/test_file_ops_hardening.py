"""
测试 file_read / file_edit 的健壮性增强
"""
import pytest

from coding_agent.tools.file_ops import FileReadTool, FileEditTool


@pytest.mark.asyncio
async def test_read_rejects_directory(tmp_path):
    out = await FileReadTool().execute(path=str(tmp_path))
    assert "is a directory" in out


@pytest.mark.asyncio
async def test_read_rejects_binary(tmp_path):
    f = tmp_path / "bin.dat"
    f.write_bytes(b"abc\x00\x01\x02def")
    out = await FileReadTool().execute(path=str(f))
    assert "binary" in out


@pytest.mark.asyncio
async def test_read_large_file_requires_slice(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("x\n" * (3 * 1024 * 1024))  # ~6MB
    out = await FileReadTool().execute(path=str(f))
    assert "too large" in out
    # 带 limit 则允许
    out2 = await FileReadTool().execute(path=str(f), offset=1, limit=2)
    assert "1 | x" in out2


@pytest.mark.asyncio
async def test_edit_ambiguous_reports_lines(tmp_path):
    f = tmp_path / "c.py"
    f.write_text("x = 1\ny = 1\nx = 1\n")
    out = await FileEditTool().execute(path=str(f), old_text="x = 1", new_text="x = 2")
    assert "found 2 times" in out
    assert "replace_all" in out


@pytest.mark.asyncio
async def test_edit_replace_all(tmp_path):
    f = tmp_path / "c.py"
    f.write_text("a = 1\na = 1\n")
    out = await FileEditTool().execute(path=str(f), old_text="a = 1",
                                       new_text="a = 2", replace_all=True)
    assert "2 occurrences" in out
    assert f.read_text() == "a = 2\na = 2\n"


@pytest.mark.asyncio
async def test_edit_identical_text_noop(tmp_path):
    f = tmp_path / "c.py"
    f.write_text("a = 1\n")
    out = await FileEditTool().execute(path=str(f), old_text="a", new_text="a")
    assert "identical" in out


@pytest.mark.asyncio
async def test_edit_unique_still_works(tmp_path):
    f = tmp_path / "c.py"
    f.write_text("hello world\n")
    out = await FileEditTool().execute(path=str(f), old_text="world", new_text="there")
    assert "Successfully edited" in out
    assert f.read_text() == "hello there\n"
