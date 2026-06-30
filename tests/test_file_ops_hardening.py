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
    assert "multiple matches" in out
    assert "near lines" in out


@pytest.mark.asyncio
async def test_edit_replace_all(tmp_path):
    f = tmp_path / "c.py"
    f.write_text("a = 1\na = 1\n")
    out = await FileEditTool().execute(path=str(f), old_text="a = 1",
                                       new_text="a = 2", replace_all=True)
    assert "Successfully edited" in out
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


@pytest.mark.asyncio
async def test_read_paginates_large_file(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"line{i}" for i in range(5000)) + "\n")
    out = await FileReadTool().execute(path=str(f))
    # 默认每页 2000 行
    assert "   1 | line0" in out
    assert "2000 | line1999" in out
    assert "line2000" not in out
    # 脚注提示下一页
    assert "more line(s)" in out
    assert "offset=2001" in out


@pytest.mark.asyncio
async def test_read_next_page(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"line{i}" for i in range(5000)) + "\n")
    out = await FileReadTool().execute(path=str(f), offset=2001)
    assert "2001 | line2000" in out
    assert "4000 | line3999" in out
    assert "offset=4001" in out


@pytest.mark.asyncio
async def test_read_small_file_no_footer(tmp_path):
    f = tmp_path / "small.py"
    f.write_text("a\nb\nc\n")
    out = await FileReadTool().execute(path=str(f))
    assert "more line(s)" not in out
    assert "   3 | c" in out


@pytest.mark.asyncio
async def test_read_explicit_limit_keeps_limit_in_footer(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("\n".join(f"L{i}" for i in range(100)) + "\n")
    out = await FileReadTool().execute(path=str(f), offset=1, limit=10)
    assert "  10 | L9" in out
    assert "L10" not in out
    assert "offset=11, limit=10" in out
