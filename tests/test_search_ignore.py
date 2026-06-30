"""
测试 grep / file_search 默认跳过噪音目录
"""
import pytest

from coding_agent.tools.file_ops import GrepTool, FileSearchTool, _is_ignored, DEFAULT_IGNORE_DIRS
from pathlib import Path


def _setup_tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("TARGET_TOKEN here\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("TARGET_TOKEN noise\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("TARGET_TOKEN gitnoise\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "c.py").write_text("TARGET_TOKEN cache\n")


def test_is_ignored():
    assert _is_ignored(Path("node_modules/x/y.py"), DEFAULT_IGNORE_DIRS)
    assert _is_ignored(Path(".git/config"), DEFAULT_IGNORE_DIRS)
    assert not _is_ignored(Path("src/app.py"), DEFAULT_IGNORE_DIRS)


@pytest.mark.asyncio
async def test_grep_skips_noise_dirs(tmp_path):
    _setup_tree(tmp_path)
    out = await GrepTool().execute(pattern="TARGET_TOKEN", path=str(tmp_path))
    assert "src/app.py" in out
    assert "node_modules" not in out
    assert ".git" not in out
    assert "__pycache__" not in out


@pytest.mark.asyncio
async def test_grep_include_ignored_opt_in(tmp_path):
    _setup_tree(tmp_path)
    out = await GrepTool().execute(pattern="TARGET_TOKEN", path=str(tmp_path),
                                   include_ignored=True)
    assert "node_modules" in out


@pytest.mark.asyncio
async def test_file_search_skips_noise_dirs(tmp_path):
    _setup_tree(tmp_path)
    out = await FileSearchTool().execute(pattern="**/*.py", root=str(tmp_path))
    assert "app.py" in out
    assert "node_modules" not in out
    assert "__pycache__" not in out
