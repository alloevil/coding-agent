"""
测试 @file 引用在后端展开：把 @path 的文件内容附到消息里（Claude Code 行为）。
"""
import os

from coding_agent import protocol as P


def _proto(tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)  # cwd = 工作区根
    proto = P.AgentProtocol.__new__(P.AgentProtocol)
    return proto


def test_expands_existing_file(tmp_path, monkeypatch):
    (tmp_path / "hello.py").write_text("print('hi')\n")
    proto = _proto(tmp_path, monkeypatch)
    out = proto._expand_file_mentions("explain @hello.py please")
    assert "explain @hello.py please" in out  # 原文保留
    assert "--- hello.py ---" in out
    assert "print('hi')" in out


def test_missing_file_left_as_literal(tmp_path, monkeypatch):
    proto = _proto(tmp_path, monkeypatch)
    out = proto._expand_file_mentions("look at @nope.py")
    assert out == "look at @nope.py"  # 不存在 → 原样


def test_no_mention_unchanged(tmp_path, monkeypatch):
    proto = _proto(tmp_path, monkeypatch)
    assert proto._expand_file_mentions("just a message") == "just a message"


def test_outside_workspace_rejected(tmp_path, monkeypatch):
    proto = _proto(tmp_path, monkeypatch)
    # 路径穿越到工作区外 → 不展开
    out = proto._expand_file_mentions("read @../../etc/passwd")
    assert "root:" not in out
    assert "passwd" in out  # 字面量保留


def test_truncates_large_file(tmp_path, monkeypatch):
    (tmp_path / "big.txt").write_text("x" * 20_000)
    proto = _proto(tmp_path, monkeypatch)
    out = proto._expand_file_mentions("@big.txt")
    assert "truncated" in out
    assert len(out) < 20_000


def test_binary_file_skipped(tmp_path, monkeypatch):
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    proto = _proto(tmp_path, monkeypatch)
    out = proto._expand_file_mentions("@bin.dat")
    assert "--- bin.dat ---" not in out


def test_caps_at_five_files(tmp_path, monkeypatch):
    for i in range(7):
        (tmp_path / f"f{i}.txt").write_text(f"content{i}")
    proto = _proto(tmp_path, monkeypatch)
    msg = " ".join(f"@f{i}.txt" for i in range(7))
    out = proto._expand_file_mentions(msg)
    expanded = out.count("--- f")
    assert expanded == 5  # 上限 5
