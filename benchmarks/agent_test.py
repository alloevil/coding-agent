"""
Agent 测试 — 用真实任务测试 coding-agent 的能力
不依赖预置 benchmark，直接用 AgentLoop 执行任务并验证结果
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from coding_agent.core.agent import AgentLoop, AgentEvent, AgentEventData, AgentConfig
from coding_agent.core.state import AgentState
from coding_agent.core.model_client import ModelClient
from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.file_ops import register_file_tools
from coding_agent.tools.shell import register_shell_tools
from coding_agent.tools.git_ops import register_git_tools
from coding_agent.tools.patch_ops import register_patch_tools
from coding_agent.tools.tdd_ops import register_tdd_tools
from coding_agent.tools.plan_ops import register_plan_tools


# ── 测试任务定义 ──

TASKS = [
    # 1. 基础文件操作：创建、写入、读取
    {
        "id": "real_file_rw",
        "difficulty": "easy",
        "task": (
            "Create a file called `greeting.py` with the following content:\n"
            "```python\n"
            "def greet(name: str) -> str:\n"
            "    return f\"Hello, {name}!\"\n"
            "\n"
            "if __name__ == \"__main__\":\n"
            "    print(greet(\"World\"))\n"
            "```\n"
            "Then run `python greeting.py` and make sure it prints 'Hello, World!'."
        ),
        "verify": lambda w: _check(w, "greeting.py", lambda c: "def greet" in c and "Hello" in c),
    },
    # 2. Bug 修复：读代码 → 找 bug → 修
    {
        "id": "real_bugfix",
        "difficulty": "medium",
        "setup": {
            "calculator.py": (
                "def divide(a, b):\n"
                "    return a / b  # Bug: no zero-check\n"
            ),
        },
        "task": (
            "Read `calculator.py`. There's a bug: `divide(10, 0)` crashes with ZeroDivisionError. "
            "Fix it so that `divide(10, 0)` returns the string `'Error: division by zero'` "
            "instead of crashing. After fixing, run: python -c \"from calculator import divide; print(divide(10,0))\" "
            "and verify the output is 'Error: division by zero'."
        ),
        "verify": lambda w: _check_run(w, "python -c \"from calculator import divide; print(divide(10,0))\"",
                                        lambda o: "Error: division by zero" in o),
    },
    # 3. 代码生成 + 测试
    {
        "id": "real_testgen",
        "difficulty": "medium",
        "setup": {
            "math_utils.py": (
                "def is_palindrome(s: str) -> bool:\n"
                "    s = s.lower().replace(' ', '')\n"
                "    return s == s[::-1]\n"
            ),
        },
        "task": (
            "Read `math_utils.py`. Write pytest tests for `is_palindrome()` in `test_math_utils.py`. "
            "Include tests for: normal palindrome, non-palindrome, empty string, single char, "
            "mixed case, string with spaces. Then run `python -m pytest test_math_utils.py -v` "
            "and make sure all tests pass."
        ),
        "verify": lambda w: _check_exists(w, "test_math_utils.py") and _check_run(
            w, "python -m pytest test_math_utils.py -v 2>&1", lambda o: "passed" in o and "failed" not in o.lower().split("passed")[0]
        ),
    },
    # 4. 多文件重构
    {
        "id": "real_refactor",
        "difficulty": "hard",
        "setup": {
            "handlers.py": (
                "import json\n\n"
                "def handle_get(path):\n"
                "    try:\n"
                "        with open(path) as f:\n"
                "            data = json.load(f)\n"
                "        return {'status': 200, 'body': data}\n"
                "    except FileNotFoundError:\n"
                "        return {'status': 404, 'body': 'Not found'}\n\n"
                "def handle_post(path, data):\n"
                "    try:\n"
                "        with open(path, 'w') as f:\n"
                "            json.dump(data, f)\n"
                "        return {'status': 201, 'body': 'Created'}\n"
                "    except Exception:\n"
                "        return {'status': 500, 'body': 'Error'}\n"
            ),
        },
        "task": (
            "Read `handlers.py`. There's duplicate error handling logic. "
            "Refactor it: extract a common `safe_execute(func, *args)` function that wraps "
            "try/except and returns the result or error dict. "
            "Rewrite `handle_get` and `handle_post` to use `safe_execute`. "
            "Keep the same external behavior — the API of handle_get/handle_post must not change. "
            "After refactoring, run: python -c \"from handlers import handle_get, handle_post; print(handle_get('/tmp/nonexist.json'))\" "
            "and verify it returns {'status': 404, 'body': 'Not found'}."
        ),
        "verify": lambda w: _check_run(
            w,
            "python -c \"from handlers import handle_get; print(handle_get('/tmp/nonexist.json'))\"",
            lambda o: "404" in o and "Not found" in o
        ),
    },
    # 5. Shell + 文件管道
    {
        "id": "real_shell",
        "difficulty": "easy",
        "task": (
            "Create a file `numbers.txt` with numbers 1 to 20, one per line. "
            "Then use a shell command to find all even numbers and save them to `even.txt`. "
            "Finally, read `even.txt` and confirm it contains 10 lines."
        ),
        "verify": lambda w: _check_exists(w, "even.txt") and _check(w, "even.txt", lambda c: len([l for l in c.strip().split('\n') if l.strip()]) == 10),
    },
    # 6. Git 操作
    {
        "id": "real_git",
        "difficulty": "medium",
        "task": (
            "Initialize a git repo. Create a file `README.md` with '# Test Project'. "
            "Commit it with message 'Initial commit'. "
            "Then create `main.py` with `print('hello')` and commit it as 'Add main.py'. "
            "Run `git log --oneline` and verify there are exactly 2 commits."
        ),
        "verify": lambda w: _check_run(w, "git log --oneline 2>&1", lambda o: o.strip().count('\n') == 1),
    },
    # 7. 多步骤项目搭建
    {
        "id": "real_project",
        "difficulty": "hard",
        "task": (
            "Create a mini todo app:\n"
            "1. `models.py` with a `Todo` dataclass (id: int, title: str, done: bool = False)\n"
            "2. `storage.py` with `save_todos(todos, path)` and `load_todos(path)` using JSON\n"
            "3. `app.py` that: loads todos, adds a todo 'Buy milk', saves, reloads, prints all todos\n"
            "Run `python app.py` and verify it prints the todo list with 'Buy milk' in it."
        ),
        "verify": lambda w: _check_run(w, "python app.py 2>&1", lambda o: "Buy milk" in o),
    },
]


def _check(workdir: str, filename: str, validator) -> tuple[bool, str]:
    path = os.path.join(workdir, filename)
    if not os.path.exists(path):
        return False, f"{filename} not found"
    content = open(path).read()
    if validator(content):
        return True, "ok"
    return False, f"content validation failed for {filename}"


def _check_exists(workdir: str, filename: str) -> bool:
    return os.path.exists(os.path.join(workdir, filename))


def _check_run(workdir: str, cmd: str, validator) -> tuple[bool, str]:
    import subprocess
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                timeout=30, cwd=workdir)
        output = result.stdout + result.stderr
        if validator(output):
            return True, "ok"
        return False, f"output validation failed: {output[:200]}"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


async def run_task(task: dict, api_key: str, model: str, base_url: str) -> dict:
    """运行单个任务"""
    workdir = tempfile.mkdtemp(prefix=f"agent_{task['id']}_")
    result = {
        "id": task["id"],
        "difficulty": task["difficulty"],
        "passed": False,
        "turns": 0,
        "time": 0.0,
        "detail": "",
        "error": None,
        "tool_calls": [],
    }

    try:
        # Setup files
        for fname, content in task.get("setup", {}).items():
            fpath = os.path.join(workdir, fname)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w") as f:
                f.write(content)

        # Build agent
        config = AgentConfig(
            model=model,
            api_key=api_key,
            api_base_url=base_url,
            max_tokens=2048,
            temperature=0.7,
            auto_approve=True,
            max_turns=15,
            stream=False,
            tool_timeout_seconds=30,
        )
        state = AgentState()
        registry = ToolRegistry()
        register_file_tools(registry)
        register_shell_tools(registry)
        register_git_tools(registry)
        register_patch_tools(registry)
        register_tdd_tools(registry)
        register_plan_tools(registry=registry)
        model_client = ModelClient(
            api_key=api_key, model=model, base_url=base_url,
            max_tokens=2048, temperature=0.7,
        )

        old_cwd = os.getcwd()
        os.chdir(workdir)

        agent = AgentLoop(config=config, tool_registry=registry)
        agent.set_model_call_fn(lambda ctx, tools: model_client.complete(ctx, tools, stream=False))

        # Run
        start = time.time()
        state = AgentState()
        async for event_data in agent.run(state, task["task"]):
            if event_data.event == AgentEvent.TOOL_CALL:
                result["tool_calls"].append(event_data.data.get("tool", "?"))
                result["turns"] += 1
            elif event_data.event == AgentEvent.ERROR:
                result["error"] = event_data.data.get("error")
            elif event_data.event == AgentEvent.DONE:
                result["turns"] += 1
        result["time"] = time.time() - start

        # Verify
        verify_fn = task["verify"]
        verify_result = verify_fn(workdir)
        if isinstance(verify_result, tuple):
            result["passed"], result["detail"] = verify_result
        else:
            result["passed"] = bool(verify_result)
            result["detail"] = "ok" if result["passed"] else "verification failed"

    except Exception as e:
        result["error"] = str(e)
        result["detail"] = f"exception: {e}"
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(workdir, ignore_errors=True)

    return result


async def main():
    api_key = os.environ.get("MODEL_API_KEY", "")
    model = os.environ.get("MODEL_PRIMARY", "gpt-4o")
    base_url = os.environ.get("MODEL_BASE_URL", "https://api.openai.com/v1")

    if not api_key:
        print("Error: set MODEL_API_KEY")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"🧪 Agent Test — Real Tasks")
    print(f"   Model: {model}")
    print(f"   Tasks: {len(TASKS)}")
    print(f"{'='*60}\n")

    results = []
    for i, task in enumerate(TASKS, 1):
        tag = f"[{i}/{len(TASKS)}] {task['id']} ({task['difficulty']})"
        print(f"{tag} ", end="", flush=True)
        r = await run_task(task, api_key, model, base_url)
        results.append(r)
        icon = "✅" if r["passed"] else "❌"
        print(f"{icon} {r['turns']}t, {r['time']:.1f}s")
        if not r["passed"]:
            print(f"         {r['detail'][:100]}")

    # Summary
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    print(f"\n{'='*60}")
    print(f"📊 Agent Test Report")
    print(f"{'='*60}")
    print(f"  Total:  {total}")
    print(f"  Passed: {passed}")
    print(f"  Failed: {total - passed}")
    print(f"  Rate:   {passed/total*100:.1f}%")
    print(f"  Avg turns: {sum(r['turns'] for r in results)/total:.1f}")
    print(f"  Avg time:  {sum(r['time'] for r in results)/total:.1f}s")
    print()

    # By difficulty
    from collections import defaultdict
    by_diff = defaultdict(lambda: {"pass": 0, "total": 0})
    for r in results:
        by_diff[r["difficulty"]]["total"] += 1
        if r["passed"]:
            by_diff[r["difficulty"]]["pass"] += 1
    for diff in ["easy", "medium", "hard"]:
        d = by_diff[diff]
        if d["total"]:
            pct = d["pass"]/d["total"]*100
            bar = "█" * int(pct/10) + "░" * (10 - int(pct/10))
            print(f"  {diff:10s} {bar} {d['pass']}/{d['total']} ({pct:.0f}%)")

    print()
    if any(not r["passed"] for r in results):
        print("  Failed:")
        for r in results:
            if not r["passed"]:
                print(f"    ❌ {r['id']}: {r['detail'][:80]}")

    # Save
    with open("/tmp/agent_test_report.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved: /tmp/agent_test_report.json")


if __name__ == "__main__":
    asyncio.run(main())
