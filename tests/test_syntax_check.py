"""
测试编辑后语法校验（Python ast.parse 警告）
"""
import pytest

from coding_agent.tools.file_ops import FileWriteTool, FileEditTool, _syntax_warning
from coding_agent.tools.patch_ops import apply_patch


def test_syntax_warning_helper():
    assert _syntax_warning("a.py", "def f():\n    return 1\n") == ""
    assert "syntax error" in _syntax_warning("a.py", "def f(:\n")
    # 非 .py 文件不校验
    assert _syntax_warning("a.txt", "def f(:\n") == ""


@pytest.mark.asyncio
async def test_file_write_warns_on_bad_python(tmp_path):
    f = tmp_path / "bad.py"
    out = await FileWriteTool().execute(path=str(f), content="def f(:\n")
    assert "Successfully wrote" in out  # 写入照常成功
    assert "syntax error" in out         # 但带警告
    assert f.exists()


@pytest.mark.asyncio
async def test_file_write_clean_python_no_warning(tmp_path):
    f = tmp_path / "ok.py"
    out = await FileWriteTool().execute(path=str(f), content="x = 1\n")
    assert "Successfully wrote" in out
    assert "Warning" not in out


@pytest.mark.asyncio
async def test_file_edit_warns_when_edit_breaks_syntax(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("def f():\n    return 1\n")
    out = await FileEditTool().execute(path=str(f), old_text="return 1", new_text="return (1")
    assert "Successfully edited" in out
    assert "syntax error" in out


@pytest.mark.asyncio
async def test_apply_patch_warns_on_bad_python(tmp_path):
    patch = "\n".join([
        "*** Begin Patch",
        "*** Add File: broken.py",
        "+def f(:",
        "*** End Patch",
    ])
    out = apply_patch(patch, root=tmp_path)
    assert "added" in out
    assert "syntax error" in out
