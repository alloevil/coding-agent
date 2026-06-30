"""
测试 grep 的 ripgrep 快路径与 Python 回退
"""
import shutil
import pytest

from coding_agent.tools.file_ops import GrepTool, _ripgrep_grep


def _setup(tmp_path):
    (tmp_path / "a.py").write_text("import os\nTARGET here\nx = 1\n")
    (tmp_path / "b.py").write_text("no match\nTARGET again\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("TARGET noise\n")


@pytest.mark.asyncio
async def test_grep_finds_matches(tmp_path):
    _setup(tmp_path)
    out = await GrepTool().execute(pattern="TARGET", path=str(tmp_path))
    assert "a.py" in out and "b.py" in out
    assert "node_modules" not in out  # 噪音目录被排除


@pytest.mark.asyncio
async def test_grep_no_match(tmp_path):
    _setup(tmp_path)
    out = await GrepTool().execute(pattern="ZZZNOPE", path=str(tmp_path))
    assert "No matches found" in out


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
async def test_ripgrep_path_used(tmp_path):
    _setup(tmp_path)
    out = await _ripgrep_grep("TARGET", str(tmp_path), None, False)
    assert out is not None
    assert "a.py" in out
    assert "node_modules" not in out


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
async def test_ripgrep_include_ignored(tmp_path):
    _setup(tmp_path)
    out = await _ripgrep_grep("TARGET", str(tmp_path), None, True)
    assert "node_modules" in out  # opt-in 包含被忽略目录


@pytest.mark.asyncio
async def test_python_fallback_when_no_rg(tmp_path, monkeypatch):
    _setup(tmp_path)
    # 让 _ripgrep_grep 认为 rg 不存在 -> execute 走 Python 回退
    monkeypatch.setattr("shutil.which", lambda name: None)
    out = await GrepTool().execute(pattern="TARGET", path=str(tmp_path))
    assert "a.py" in out and "b.py" in out
    assert "node_modules" not in out
