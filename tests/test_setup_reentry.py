"""
测试 --setup 命令行标志 + /setup slash 命令(重新引导配置)。
"""
from coding_agent.main import _parse_args
from coding_agent.core.commands import dispatch, CommandContext


def test_parse_setup_flag():
    opts = _parse_args(["--setup"])
    assert opts["setup"] is True


def test_parse_no_setup_default():
    opts = _parse_args([])
    assert opts["setup"] is False


def test_parse_setup_with_other_flags():
    opts = _parse_args(["--tui", "--setup"])
    assert opts["setup"] is True and opts["tui"] is True


def test_slash_setup_dispatch():
    r = dispatch("/setup", CommandContext(tool_names=[]))
    assert r.kind == "action"
    assert r.payload == "setup"
