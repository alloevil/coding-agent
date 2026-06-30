"""
测试 LSP didChange 刷新（编辑后内容同步）+ 新工具注册。

不依赖真 LSP server：用一个记录 notification 的假 client 验证 didOpen/didChange
的发送逻辑与 version 递增。
"""
import asyncio

import pytest

from coding_agent.tools.lsp_ops import _open_text_document, register_lsp_tools


class _FakeClient:
    """记录 send_notification 的假 LSP client。"""
    def __init__(self):
        self._open_docs = {}
        self.notifications = []

    def send_notification(self, method, params=None):
        self.notifications.append((method, params))


def test_first_sync_sends_didopen(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("x = 1\n", encoding="utf-8")
    c = _FakeClient()
    asyncio.run(_open_text_document(c, str(f)))
    methods = [m for m, _ in c.notifications]
    assert methods == ["textDocument/didOpen"]
    uri = f"file://{f.resolve()}"
    assert c._open_docs[uri][0] == 1


def test_unchanged_no_resend(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("x = 1\n", encoding="utf-8")
    c = _FakeClient()
    asyncio.run(_open_text_document(c, str(f)))
    asyncio.run(_open_text_document(c, str(f)))  # 内容没变
    methods = [m for m, _ in c.notifications]
    # 只发了一次 didOpen，第二次内容相同 → 不重发
    assert methods == ["textDocument/didOpen"]


def test_edit_triggers_didchange(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("x = 1\n", encoding="utf-8")
    c = _FakeClient()
    asyncio.run(_open_text_document(c, str(f)))
    # 模拟编辑
    f.write_text("x = 2\n", encoding="utf-8")
    asyncio.run(_open_text_document(c, str(f)))
    methods = [m for m, _ in c.notifications]
    assert methods == ["textDocument/didOpen", "textDocument/didChange"]
    # version 递增到 2，内容是新内容
    _, change = c.notifications[1]
    assert change["textDocument"]["version"] == 2
    assert change["contentChanges"][0]["text"] == "x = 2\n"
    uri = f"file://{f.resolve()}"
    assert c._open_docs[uri][0] == 2


def test_new_tools_registered():
    from coding_agent.tools.registry import ToolRegistry
    reg = ToolRegistry()
    register_lsp_tools(reg)
    assert reg.get_tool("lsp_implementation") is not None
    assert reg.get_tool("lsp_workspace_symbols") is not None
    # 原有工具仍在
    assert reg.get_tool("lsp_goto_definition") is not None
