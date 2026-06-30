"""
测试超长工具结果溢出到磁盘：预览含路径，全文可读回。
"""
from pathlib import Path

from coding_agent.tools.registry import ToolRegistry


def test_short_result_not_truncated():
    reg = ToolRegistry(max_result_chars=1000)
    out = reg._truncate_result("short")
    assert out == "short"


def test_long_result_truncated_with_spill(tmp_path):
    reg = ToolRegistry(max_result_chars=100)
    reg._spill_dir = str(tmp_path / "spill")
    full = "A" * 50 + "B" * 200 + "C" * 50  # 300 chars > 100
    out = reg._truncate_result(full)
    assert "truncated by tool-output limit" in out
    assert "full output saved to" in out
    # 预览比原文短
    assert len(out) < len(full) + 200


def test_spill_file_has_full_content(tmp_path):
    reg = ToolRegistry(max_result_chars=100)
    reg._spill_dir = str(tmp_path / "spill")
    full = "X" * 500
    reg._truncate_result(full)
    # 溢出目录里应有一个含全文的文件
    files = list((tmp_path / "spill").glob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == full


def test_spill_path_is_readable_back(tmp_path):
    import re
    reg = ToolRegistry(max_result_chars=100)
    reg._spill_dir = str(tmp_path / "spill")
    full = "line\n" * 200
    out = reg._truncate_result(full)
    m = re.search(r"full output saved to (\S+)", out)
    assert m
    saved = Path(m.group(1))
    assert saved.read_text(encoding="utf-8") == full


def test_cap_zero_disables_truncation():
    reg = ToolRegistry(max_result_chars=0)
    big = "Z" * 100000
    assert reg._truncate_result(big) == big
