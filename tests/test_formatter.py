"""
测试写后自动格式化（core/formatter.py）。

用一个临时目录里的假格式化器(放进 PATH)做端到端验证，不依赖真工具。
"""
import os
import stat

from coding_agent.core import formatter
from coding_agent.core.formatter import format_file, set_enabled, _pick_command


def _install_fake_formatter(bin_dir, name, behavior="uppercase"):
    """在 bin_dir 放一个可执行假格式化器；就地把目标文件改成大写。"""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / name
    # 假 ruff: `ruff format <FILE>` → 把文件内容转大写
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "f = sys.argv[-1]\n"
        "data = open(f).read()\n"
        "open(f,'w').write(data.upper())\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return script


def test_pick_command_by_extension():
    # 不依赖真工具：只验证扩展名映射存在
    assert ".py" in formatter._FORMATTERS
    assert ".go" in formatter._FORMATTERS
    assert formatter._FORMATTERS[".go"][0][0] == "gofmt"


def test_unknown_extension_no_format(tmp_path):
    f = tmp_path / "data.xyz"
    f.write_text("hello", encoding="utf-8")
    assert format_file(str(f)) == ""


def test_format_runs_fake_ruff(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    _install_fake_formatter(bin_dir, "ruff")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    set_enabled(True)

    f = tmp_path / "code.py"
    f.write_text("x = 1\n", encoding="utf-8")
    note = format_file(str(f))
    assert "Formatted with ruff" in note
    assert f.read_text(encoding="utf-8") == "X = 1\n"  # 被假 ruff 改成大写


def test_disabled_skips(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    _install_fake_formatter(bin_dir, "ruff")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    set_enabled(False)
    try:
        f = tmp_path / "code.py"
        f.write_text("x = 1\n", encoding="utf-8")
        assert format_file(str(f)) == ""
        assert f.read_text(encoding="utf-8") == "x = 1\n"  # 未改动
    finally:
        set_enabled(True)


def test_missing_file_no_error(tmp_path):
    assert format_file(str(tmp_path / "nope.py")) == ""


def test_no_formatter_installed_skips(tmp_path, monkeypatch):
    # 清空 PATH → which 全失败 → 跳过
    monkeypatch.setenv("PATH", "")
    set_enabled(True)
    f = tmp_path / "code.py"
    f.write_text("x=1\n", encoding="utf-8")
    assert format_file(str(f)) == ""


def test_edit_tool_triggers_format(tmp_path, monkeypatch):
    import asyncio
    from coding_agent.tools.file_ops import FileEditTool
    bin_dir = tmp_path / "bin"
    _install_fake_formatter(bin_dir, "ruff")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    set_enabled(True)

    f = tmp_path / "m.py"
    f.write_text("a = 1\nb = 2\n", encoding="utf-8")
    out = asyncio.run(FileEditTool().execute(path=str(f), old_text="b = 2", new_text="b = 3"))
    assert "Formatted with ruff" in out
    assert f.read_text(encoding="utf-8") == "A = 1\nB = 3\n"
