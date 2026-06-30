"""
测试 tdd_fix_loop 的真实闭环（模型驱动修复）。

用一个桩模型（返回 <<<FILE>>> 格式的修正文件）验证：跑测试→喂失败给模型→
应用修正→重跑→通过。不需要真模型。
"""
import asyncio
import json

import pytest

from coding_agent.tools.tdd_ops import TddFixLoopTool


def _setup_buggy(tmp_path):
    # 一个有 bug 的实现 + 一个会失败的测试
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "calc_test.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )


class _StubParent:
    """带 _model_call_fn 的假父 agent。"""
    def __init__(self, fn):
        self._model_call_fn = fn


def test_fix_loop_repairs_bug(tmp_path):
    _setup_buggy(tmp_path)
    calls = {"n": 0}

    async def model(messages, tools):
        calls["n"] += 1
        # 模型返回修正后的 calc.py（把 - 改成 +）
        return {"content": "<<<FILE: calc.py>>>\ndef add(a, b):\n    return a + b\n<<<END>>>"}

    tool = TddFixLoopTool(parent_agent=_StubParent(model))
    out = asyncio.run(tool.execute(workdir=str(tmp_path), framework="pytest",
                                   source_files=["calc.py"], max_iterations=3))
    summary = json.loads(out)
    assert summary["fixed"] is True
    assert summary["final_failed"] == 0
    assert calls["n"] >= 1
    # 文件确实被改对了
    assert "a + b" in (tmp_path / "calc.py").read_text()


def test_fix_loop_refuses_to_edit_tests(tmp_path):
    _setup_buggy(tmp_path)

    async def model(messages, tools):
        # 恶意/错误地试图改测试文件 → 应被拒绝
        return {"content": "<<<FILE: calc_test.py>>>\ndef test_add():\n    assert True\n<<<END>>>"}

    tool = TddFixLoopTool(parent_agent=_StubParent(model))
    out = asyncio.run(tool.execute(workdir=str(tmp_path), framework="pytest",
                                   max_iterations=2))
    summary = json.loads(out)
    # 测试没被篡改，仍然失败
    assert summary["fixed"] is False
    assert "assert add(2, 3) == 5" in (tmp_path / "calc_test.py").read_text()


def test_fix_loop_no_model_returns_failures(tmp_path):
    _setup_buggy(tmp_path)
    tool = TddFixLoopTool(parent_agent=None)  # 无模型
    out = asyncio.run(tool.execute(workdir=str(tmp_path), framework="pytest"))
    summary = json.loads(out)
    assert summary["fixed"] is False
    assert summary["iterations"][0]["action"] == "no_model_returned_failures"


def test_fix_loop_already_passing(tmp_path):
    (tmp_path / "ok.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "ok_test.py").write_text(
        "from ok import f\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8")

    async def model(messages, tools):
        raise AssertionError("model should not be called when tests already pass")

    tool = TddFixLoopTool(parent_agent=_StubParent(model))
    out = asyncio.run(tool.execute(workdir=str(tmp_path), framework="pytest"))
    summary = json.loads(out)
    assert summary["fixed"] is True
    assert summary["total_iterations"] == 0  # 没进修复循环


def test_fix_loop_stops_when_model_gives_no_edit(tmp_path):
    _setup_buggy(tmp_path)

    async def model(messages, tools):
        return {"content": "I cannot fix this."}  # 无 <<<FILE>>> 块

    tool = TddFixLoopTool(parent_agent=_StubParent(model))
    out = asyncio.run(tool.execute(workdir=str(tmp_path), framework="pytest",
                                   max_iterations=5))
    summary = json.loads(out)
    assert summary["fixed"] is False
    # 模型没给修改 → 停止，不空转到 5 轮
    assert summary["total_iterations"] == 1


def test_register_passes_parent():
    from coding_agent.tools.registry import ToolRegistry
    from coding_agent.tools.tdd_ops import register_tdd_tools
    reg = ToolRegistry()
    sentinel = object()
    register_tdd_tools(reg, parent_agent=sentinel)
    tool = reg.get_tool("tdd_fix_loop")
    assert tool._parent_agent is sentinel
