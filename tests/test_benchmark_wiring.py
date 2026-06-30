"""
离线验证 benchmark 走真实 agent 路径（无需 API key）。

用脚本化 mock 替换 ModelClient.complete，确认：
- benchmark 用的是改进后的完整工具集（apply_patch / update_plan 可用）
- run_case 的真实执行 + verify 流程跑通
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "benchmarks"))
import benchmark as B  # noqa: E402


@pytest.mark.asyncio
async def test_benchmark_runs_real_agent_with_new_tools():
    runner = B.BenchmarkRunner(api_key="x", model="mock", base_url="http://x/v1")

    turns = [
        {"content": "", "tool_calls": [{"id": "1", "type": "function",
            "function": {"name": "update_plan",
                "arguments": '{"steps":[{"step":"create","status":"in_progress"}]}'}}]},
        {"content": "", "tool_calls": [{"id": "2", "type": "function",
            "function": {"name": "apply_patch",
                "arguments": '{"patch":"*** Begin Patch\\n*** Add File: hello.txt\\n'
                             '+Hello, World!\\n*** End Patch"}'}}]},
        {"content": "done", "tool_calls": []},
    ]
    idx = {"i": 0}

    async def fake_complete(context, tools, stream=False, on_text_delta=None):
        r = turns[min(idx["i"], len(turns) - 1)]
        idx["i"] += 1
        return r

    runner.model_client.complete = fake_complete

    case = next(c for c in B.BENCHMARK_CASES if c.id == "file_01")
    result = await runner.run_case(case)

    assert "update_plan" in result.tool_calls
    assert "apply_patch" in result.tool_calls
    assert result.passed, result.detail
