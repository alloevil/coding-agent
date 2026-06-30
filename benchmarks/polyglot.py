"""
Aider polyglot 基准 - 用真实 Exercism 题库评测 coding agent

参考 Aider 的 polyglot benchmark：每道题给 agent 一个待实现的 stub + 题面，
agent 用工具编辑代码，然后跑该题自带的隐藏测试套件判分（不是我们自己写的
verify —— 客观、可对标公开 leaderboard）。

题库：https://github.com/Aider-AI/polyglot-benchmark （MIT，需先 clone）
默认从 $POLYGLOT_DIR 或 /tmp/polyglot-benchmark 读取。

用法：
  POLYGLOT_DIR=/tmp/polyglot-probe \
  ANTHROPIC_BASE_URL=... ANTHROPIC_AUTH_TOKEN=... \
  python benchmarks/polyglot.py --lang python --limit 20 --model claude-opus-4-8

验证：把题目的 solution+test 文件复制到临时目录（不给 .meta/example），
agent 改完后跑 pytest（python）/ 对应语言的 test runner，全过即通过。
当前实现支持 python（其余语言留待扩展）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coding_agent.core.agent import AgentLoop, AgentEvent
from coding_agent.core.config import AgentConfig
from coding_agent.core.state import AgentState
from coding_agent.core.model_client import ModelClient
from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.file_ops import register_file_tools
from coding_agent.tools.shell import register_shell_tools
from coding_agent.tools.tdd_ops import register_tdd_tools
from coding_agent.tools.plan_ops import register_plan_tools
from coding_agent.tools.patch_ops import register_patch_tools


# 各语言的测试运行命令（在题目工作目录里执行）
def _run_python_tests(workdir: Path, test_files: list[str]) -> tuple[bool, str]:
    cmd = [sys.executable, "-m", "pytest", "-q", *test_files]
    proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=120)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]


def _discover_exercises(lang_dir: Path) -> list[Path]:
    practice = lang_dir / "exercises" / "practice"
    if not practice.is_dir():
        return []
    return sorted(p for p in practice.iterdir() if p.is_dir())


def _load_exercise(ex_dir: Path) -> dict | None:
    """读题目元数据：solution/test 文件名 + 题面。example 不读（不给 agent）。"""
    cfg_path = ex_dir / ".meta" / "config.json"
    if not cfg_path.is_file():
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    files = cfg.get("files", {})
    solution = files.get("solution", [])
    test = files.get("test", [])
    if not solution or not test:
        return None
    instr = ""
    for name in ("instructions.md", "instructions.append.md"):
        p = ex_dir / ".docs" / name
        if p.is_file():
            instr += p.read_text(encoding="utf-8") + "\n\n"
    return {"name": ex_dir.name, "dir": ex_dir,
            "solution": solution, "test": test, "instructions": instr}


def _build_prompt(ex: dict) -> str:
    sol = ", ".join(ex["solution"])
    test = ", ".join(ex["test"])
    return (
        f"Solve this coding exercise by editing the solution file(s): {sol}.\n"
        f"The test file(s) {test} define the requirements — make all tests pass. "
        f"Do NOT edit the test files. When done, the test suite must pass.\n\n"
        f"## Exercise\n\n{ex['instructions']}"
    )


class PolyglotRunner:
    def __init__(self, api_key: str, base_url: str, model: str,
                 protocol: str, extra_headers: dict, max_turns: int = 30):
        self.model = model
        self.max_turns = max_turns
        self.model_client = ModelClient(
            api_key=api_key, base_url=base_url, model=model,
            protocol=protocol, temperature=None, max_tokens=4096,
            extra_headers=extra_headers,
        )

    async def _call_model(self, context, tools):
        return await self.model_client.complete(context, tools, stream=False)

    async def run_exercise(self, ex: dict) -> dict:
        workdir = Path(tempfile.mkdtemp(prefix=f"poly_{ex['name']}_"))
        # 只复制 solution + test 文件（不给 example/.meta）
        for fname in ex["solution"] + ex["test"]:
            src = ex["dir"] / fname
            if src.is_file():
                dst = workdir / fname
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        registry = ToolRegistry()
        register_file_tools(registry)
        register_shell_tools(registry)
        register_tdd_tools(registry)
        plan_tool = register_plan_tools(registry=registry)
        register_patch_tools(registry)

        old_cwd = os.getcwd()
        os.chdir(workdir)
        start = time.time()
        try:
            config = AgentConfig(
                model=self.model, api_key="x", max_turns=self.max_turns,
                auto_approve=True,
                system_prompt=(
                    f"You are a coding agent working in {workdir}. Implement the "
                    f"solution so the existing test suite passes. Edit only the "
                    f"solution file(s); never modify the tests. Verify by running the "
                    f"tests yourself (tdd_run_tests or pytest) before finishing."
                ),
            )
            agent = AgentLoop(config=config, tool_registry=registry)
            agent.set_model_call_fn(self._call_model)
            state = AgentState()
            plan_tool.bind_state(state)
            turns = 0
            try:
                async for event in agent.run(state, _build_prompt(ex)):
                    if event.event == AgentEvent.DONE:
                        turns = event.data.get("turns", 0)
            except Exception as e:  # noqa: BLE001
                return {"name": ex["name"], "passed": False,
                        "error": f"agent error: {type(e).__name__}: {e}",
                        "turns": turns, "seconds": time.time() - start}

            # 跑测试判分
            try:
                passed, output = _run_python_tests(workdir, ex["test"])
            except subprocess.TimeoutExpired:
                passed, output = False, "test timeout"
            return {"name": ex["name"], "passed": passed,
                    "turns": turns, "seconds": time.time() - start,
                    "output": "" if passed else output[-500:]}
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(workdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="python")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--model", default=os.environ.get("POLYGLOT_MODEL", "claude-opus-4-8"))
    args = ap.parse_args()

    poly_dir = Path(os.environ.get("POLYGLOT_DIR", "/tmp/polyglot-benchmark"))
    lang_dir = poly_dir / args.lang
    if not lang_dir.is_dir():
        print(f"Error: {lang_dir} not found. Clone the polyglot benchmark first:")
        print("  git clone https://github.com/Aider-AI/polyglot-benchmark.git")
        sys.exit(1)

    # 端点：优先 Anthropic（ANTHROPIC_BASE_URL/TOKEN），否则 OpenAI 兼容
    if os.environ.get("ANTHROPIC_BASE_URL"):
        base_url = os.environ["ANTHROPIC_BASE_URL"].rstrip("/")
        key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        protocol = "anthropic"
        extra_headers = {"Authorization": f"Bearer {key}"}
    else:
        key = os.environ.get("MODEL_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        base_url = os.environ.get("MODEL_BASE_URL", "https://api.openai.com/v1")
        protocol = "openai"
        extra_headers = {}
    if not key:
        print("Error: set ANTHROPIC_AUTH_TOKEN (+ ANTHROPIC_BASE_URL) or MODEL_API_KEY")
        sys.exit(1)

    exercises = [_load_exercise(d) for d in _discover_exercises(lang_dir)]
    exercises = [e for e in exercises if e][: args.limit]
    print(f"Polyglot benchmark: {args.lang}, {len(exercises)} exercises, model={args.model}")
    print("=" * 60)

    runner = PolyglotRunner(key, base_url, args.model, protocol, extra_headers)

    async def run_all():
        results = []
        for i, ex in enumerate(exercises, 1):
            r = await runner.run_exercise(ex)
            mark = "✅ PASS" if r["passed"] else "❌ FAIL"
            extra = "" if r["passed"] else f"  ({r.get('error') or 'tests failed'})"
            print(f"[{i}/{len(exercises)}] {ex['name']:24} {mark} "
                  f"({r.get('turns', 0)}t, {r['seconds']:.0f}s){extra}")
            results.append(r)
        return results

    results = asyncio.run(run_all())
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    print("=" * 60)
    print(f"Pass rate: {passed}/{total} = {passed/total*100:.1f}%" if total else "no exercises")
    report = Path(__file__).parent / "polyglot_report.json"
    report.write_text(json.dumps({"model": args.model, "lang": args.lang,
                                  "passed": passed, "total": total,
                                  "results": results}, indent=2), encoding="utf-8")
    print(f"Report saved to {report}")


if __name__ == "__main__":
    main()
