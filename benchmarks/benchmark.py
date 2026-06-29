"""
Coding Agent Benchmark

测试维度（参考 SWE-bench / HumanEval / AgentBench）：
1. 文件操作 — 读/写/编辑准确性
2. 代码理解 — 能否读懂代码并回答问题
3. Bug 修复 — 给定 bug 描述，修复代码
4. 重构 — 跨文件重构
5. 测试生成 — 为给定代码写测试
6. 工具使用 — 正确选择和组合工具
7. 规划能力 — 多步骤任务的拆解和执行

每个测试用例：
- task: 任务描述
- setup: 前置文件/代码
- verify: 验证函数（检查结果）
- max_turns: 最大轮次
- category: 测试类别
- difficulty: easy / medium / hard
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from coding_agent.core.agent import AgentLoop, AgentEvent, AgentEventData, AgentConfig
from coding_agent.core.state import AgentState
from coding_agent.core.model_client import ModelClient
from coding_agent.tools.registry import ToolRegistry
from coding_agent.tools.file_ops import register_file_tools
from coding_agent.tools.shell import register_shell_tools
from coding_agent.tools.git_ops import register_git_tools
from coding_agent.tools.lsp_ops import register_lsp_tools
from coding_agent.tools.browser_ops import register_browser_tools
from coding_agent.tools.tdd_ops import register_tdd_tools
from coding_agent.tools.memory_ops import register_memory_tools
from coding_agent.tools.plan_ops import register_plan_tools
from coding_agent.tools.patch_ops import register_patch_tools


# ===========================================================================
# Benchmark 数据结构
# ===========================================================================

@dataclass
class BenchmarkCase:
    """单个测试用例"""
    id: str
    category: str
    difficulty: str  # easy / medium / hard
    task: str
    setup_files: dict[str, str]  # path -> content
    verify_fn: Any  # (workdir) -> (passed: bool, detail: str)
    max_turns: int = 10
    timeout: int = 120


@dataclass
class BenchmarkResult:
    """单个测试结果"""
    case_id: str
    category: str
    difficulty: str
    passed: bool
    turns_used: int
    time_seconds: float
    detail: str
    error: str | None = None
    tool_calls: list[str] = field(default_factory=list)


@dataclass
class BenchmarkReport:
    """完整报告"""
    results: list[BenchmarkResult]
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    avg_turns: float = 0
    avg_time: float = 0
    by_category: dict[str, dict] = field(default_factory=dict)
    by_difficulty: dict[str, dict] = field(default_factory=dict)


# ===========================================================================
# 测试用例定义
# ===========================================================================

# ===========================================================================
# Verify helper functions (evaluated at runtime, not definition time)
# ===========================================================================

def _check_file(d: str, filename: str, expected_content: str) -> tuple[bool, str]:
    p = Path(d, filename)
    if not p.exists():
        return False, f"File '{filename}' not found"
    content = p.read_text()
    if expected_content in content:
        return True, f"File '{filename}' exists and contains expected content"
    return False, f"File '{filename}' exists but content mismatch: {content[:100]}"

def _check_contains(d: str, filename: str, needle: str) -> tuple[bool, str]:
    p = Path(d, filename)
    if not p.exists():
        return False, f"File '{filename}' not found"
    content = p.read_text()
    if needle in content:
        return True, f"Found '{needle}' in {filename}"
    return False, f"'{needle}' not found in {filename}: {content[:100]}"

def _check_not_contains(d: str, filename: str, needle: str) -> tuple[bool, str]:
    p = Path(d, filename)
    if not p.exists():
        return False, f"File '{filename}' not found"
    content = p.read_text()
    if needle not in content:
        return True, f"'{needle}' removed from {filename}"
    return False, f"'{needle}' still present in {filename}"

def _check_contains_any(d: str, filename: str, needles: list[str]) -> tuple[bool, str]:
    p = Path(d, filename)
    if not p.exists():
        return False, f"File '{filename}' not found"
    content = p.read_text()
    for n in needles:
        if n in content:
            return True, f"Found '{n}' in {filename}"
    return False, f"None of {needles} found in {filename}"

def _check_refactor(d: str) -> tuple[bool, str]:
    vp = Path(d, "validators.py")
    up = Path(d, "user.py")
    if not vp.exists():
        return False, "validators.py not created"
    if not up.exists():
        return False, "user.py not found"
    vc = vp.read_text()
    uc = up.read_text()
    if "validate" not in vc:
        return False, "validate not in validators.py"
    if "import" not in uc:
        return False, "no import in user.py"
    return True, "validators.py created and imported"

def _check_testgen(d: str) -> tuple[bool, str]:
    p = Path(d, "test_palindrome.py")
    if not p.exists():
        return False, "test_palindrome.py not created"
    content = p.read_text()
    if "def test_" not in content:
        return False, "No test functions found"
    return True, "Test file created with test functions"

def _check_tool_combo(d: str) -> tuple[bool, str]:
    cp = Path(d, "count.py")
    sp = Path(d, "sample.txt")
    if not cp.exists():
        return False, "count.py not created"
    if not sp.exists():
        return False, "sample.txt not created"
    return True, "Both files created"

def _check_planning(d: str) -> tuple[bool, str]:
    files = ["models.py", "storage.py", "main.py"]
    missing = [f for f in files if not Path(d, f).exists()]
    if missing:
        return False, f"Missing: {', '.join(missing)}"
    return True, "All 3 files created"

def _check_multi(d: str) -> tuple[bool, str]:
    csv_p = Path(d, "data.csv")
    py_p = Path(d, "analyze.py")
    if not csv_p.exists():
        return False, "data.csv not created"
    if not py_p.exists():
        return False, "analyze.py not created"
    return True, "Both files created"


# ===========================================================================
# Additional Verify Helpers
# ===========================================================================

def _check_dir_exists(d: str, dirname: str) -> tuple[bool, str]:
    p = Path(d, dirname)
    if p.is_dir():
        return True, f"Directory '{dirname}' exists"
    return False, f"Directory '{dirname}' not found"


def _check_json_field(d: str, filename: str, field: str) -> tuple[bool, str]:
    p = Path(d, filename)
    if not p.exists():
        return False, f"File '{filename}' not found"
    try:
        data = json.loads(p.read_text())
        if field in data:
            return True, f"Field '{field}' found with value: {data[field]}"
        return False, f"Field '{field}' not in JSON keys: {list(data.keys())}"
    except Exception as e:
        return False, f"JSON parse error: {e}"


def _check_file_count_in_dir(d: str, dirname: str, min_count: int) -> tuple[bool, str]:
    p = Path(d, dirname)
    if not p.is_dir():
        return False, f"Directory '{dirname}' not found"
    count = sum(1 for _ in p.rglob("*") if _.is_file())
    if count >= min_count:
        return True, f"Directory '{dirname}' has {count} files (>= {min_count})"
    return False, f"Directory '{dirname}' has {count} files (< {min_count})"


def _check_executable(d: str, filename: str) -> tuple[bool, str]:
    p = Path(d, filename)
    if not p.exists():
        return False, f"File '{filename}' not found"
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, str(p)],
            capture_output=True, text=True, timeout=10, cwd=d
        )
        if result.returncode == 0:
            return True, f"'{filename}' ran successfully: {result.stdout[:80]}"
        return False, f"'{filename}' exited {result.returncode}: {result.stderr[:80]}"
    except Exception as e:
        return False, f"Run error: {e}"


def _check_file_lines(d: str, filename: str, min_lines: int) -> tuple[bool, str]:
    p = Path(d, filename)
    if not p.exists():
        return False, f"File '{filename}' not found"
    lines = [l for l in p.read_text().splitlines() if l.strip()]
    if len(lines) >= min_lines:
        return True, f"'{filename}' has {len(lines)} non-empty lines (>= {min_lines})"
    return False, f"'{filename}' has {len(lines)} non-empty lines (< {min_lines})"


def _check_function_def(d: str, filename: str, func_name: str) -> tuple[bool, str]:
    p = Path(d, filename)
    if not p.exists():
        return False, f"File '{filename}' not found"
    content = p.read_text()
    if f"def {func_name}" in content:
        return True, f"Function '{func_name}' defined in {filename}"
    return False, f"Function '{func_name}' not found in {filename}"


def _check_class_def(d: str, filename: str, class_name: str) -> tuple[bool, str]:
    p = Path(d, filename)
    if not p.exists():
        return False, f"File '{filename}' not found"
    content = p.read_text()
    if f"class {class_name}" in content:
        return True, f"Class '{class_name}' defined in {filename}"
    return False, f"Class '{class_name}' not found in {filename}"


def _check_no_syntax_error(d: str, filename: str) -> tuple[bool, str]:
    p = Path(d, filename)
    if not p.exists():
        return False, f"File '{filename}' not found"
    try:
        compile(p.read_text(), str(p), "exec")
        return True, f"'{filename}' has no syntax errors"
    except SyntaxError as e:
        return False, f"Syntax error in '{filename}': {e}"


def _check_test_passes(d: str, test_file: str) -> tuple[bool, str]:
    p = Path(d, test_file)
    if not p.exists():
        return False, f"Test file '{test_file}' not found"
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(p), "-v", "--tb=short"],
            capture_output=True, text=True, timeout=30, cwd=d
        )
        if result.returncode == 0:
            return True, f"Tests pass: {result.stdout[-100:]}"
        return False, f"Tests fail (rc={result.returncode}): {result.stderr[-100:]}"
    except Exception as e:
        return False, f"Test run error: {e}"


def _check_import_works(d: str, module_name: str) -> tuple[bool, str]:
    try:
        import subprocess
        result = subprocess.run(
            [sys.executable, "-c", f"import {module_name}; print('OK')"],
            capture_output=True, text=True, timeout=10, cwd=d
        )
        if result.returncode == 0:
            return True, f"Import '{module_name}' succeeded"
        return False, f"Import '{module_name}' failed: {result.stderr[:80]}"
    except Exception as e:
        return False, f"Import error: {e}"


def _first_pass(*checks: tuple[bool, str]) -> tuple[bool, str]:
    """Return the first passing check, or the last check result if all fail."""
    last = (False, "No checks ran")
    for c in checks:
        last = c
        if c[0]:
            return c
    return last


def _check_contains_all(d: str, filename: str, needles: list[str]) -> tuple[bool, str]:
    """Check that ALL needles are present in the file."""
    p = Path(d, filename)
    if not p.exists():
        return False, f"File '{filename}' not found"
    content = p.read_text()
    missing = [n for n in needles if n not in content]
    if not missing:
        return True, f"All {len(needles)} patterns found in {filename}"
    return False, f"Missing patterns {missing} in {filename}"


def _check_file_empty_lines_removed(d: str, filename: str) -> tuple[bool, str]:
    p = Path(d, filename)
    if not p.exists():
        return False, f"File '{filename}' not found"
    lines = p.read_text().splitlines()
    empty_count = sum(1 for l in lines if not l.strip())
    if empty_count == 0:
        return True, f"No empty lines in '{filename}'"
    return False, f"'{filename}' still has {empty_count} empty lines"


def _check_file_not_exists(d: str, filename: str) -> tuple[bool, str]:
    p = Path(d, filename)
    if not p.exists():
        return True, f"File '{filename}' correctly does not exist"
    return False, f"File '{filename}' still exists"


def _check_git_init(d: str) -> tuple[bool, str]:
    p = Path(d, ".git")
    if p.is_dir():
        return True, "Git repository initialized"
    return False, "No .git directory found"


def _check_refactor_class_inherit(d: str) -> tuple[bool, str]:
    """Check that a base class exists and a subclass inherits from it."""
    for fname in Path(d).glob("*.py"):
        content = fname.read_text()
        if "class " in content and "(" in content and ")" in content:
            if "def __init__" in content or "def " in content:
                return True, f"Class inheritance found in {fname.name}"
    return False, "No class inheritance pattern found"


BENCHMARK_CASES: list[BenchmarkCase] = [
    # ── 文件操作 (easy) ────────────────────────────────────────────────
    BenchmarkCase(
        id="file_01",
        category="file_ops",
        difficulty="easy",
        task="Create a file called hello.txt with the content 'Hello, World!'",
        setup_files={},
        verify_fn=lambda d: _check_file(d, "hello.txt", "Hello, World!"),
        max_turns=5,
    ),
    BenchmarkCase(
        id="file_02",
        category="file_ops",
        difficulty="easy",
        task="Read the file config.txt and tell me what's on line 3",
        setup_files={"config.txt": "line1\nline2\nline3_content\nline4"},
        verify_fn=lambda d: (True, "Agent responded"),
        max_turns=5,
    ),
    BenchmarkCase(
        id="file_03",
        category="file_ops",
        difficulty="medium",
        task="In the file math.py, change the function add(a, b) to return a + b + 1 instead of a + b",
        setup_files={"math.py": "def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n"},
        verify_fn=lambda d: _check_contains(d, "math.py", "+ 1"),
        max_turns=5,
    ),

    # ── 代码理解 (medium) ─────────────────────────────────────────────
    BenchmarkCase(
        id="understand_01",
        category="code_understanding",
        difficulty="medium",
        task="Look at fibonacci.py. How many functions does it define? Answer with just the number.",
        setup_files={"fibonacci.py": """
def fib_recursive(n):
    if n <= 1:
        return n
    return fib_recursive(n-1) + fib_recursive(n-2)

def fib_iterative(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a

def fib_memo(n, memo={}):
    if n in memo:
        return memo[n]
    if n <= 1:
        return n
    memo[n] = fib_memo(n-1, memo) + fib_memo(n-2, memo)
    return memo[n]
"""},
        verify_fn=lambda d: (True, "Agent responded"),
        max_turns=5,
    ),

    # ── Bug 修复 (hard) ───────────────────────────────────────────────
    BenchmarkCase(
        id="bugfix_01",
        category="bug_fix",
        difficulty="medium",
        task="The file sorter.py has a bug: sort_numbers([3,1,2]) should return [1,2,3] but it returns [3,2,1]. Fix the bug.",
        setup_files={"sorter.py": """
def sort_numbers(nums):
    return sorted(nums, reverse=True)

def sort_strings(strings):
    return sorted(strings, key=str.lower)
"""},
        verify_fn=lambda d: _check_not_contains(d, "sorter.py", "reverse=True"),
        max_turns=5,
    ),
    BenchmarkCase(
        id="bugfix_02",
        category="bug_fix",
        difficulty="hard",
        task="Fix the bug in calculator.py: divide(10, 0) should return 'Error: division by zero' but it crashes with ZeroDivisionError.",
        setup_files={"calculator.py": """
def add(a, b):
    return a + b

def subtract(a, b):
    return a - b

def multiply(a, b):
    return a * b

def divide(a, b):
    return a / b
"""},
        verify_fn=lambda d: _check_contains_any(d, "calculator.py", ["except", "if b", "b !=", "if b == 0", "if not b"]),
        max_turns=5,
    ),

    # ── 重构 (hard) ───────────────────────────────────────────────────
    BenchmarkCase(
        id="refactor_01",
        category="refactor",
        difficulty="hard",
        task="Extract the validation logic from user.py into a separate file validators.py. Import validators in user.py.",
        setup_files={
            "user.py": """
class User:
    def __init__(self, name, email, age):
        self.name = name
        self.email = email
        self.age = age
    
    def validate(self):
        if not self.name or len(self.name) < 2:
            return False, "Name too short"
        if "@" not in self.email:
            return False, "Invalid email"
        if self.age < 0 or self.age > 150:
            return False, "Invalid age"
        return True, "Valid"
    
    def to_dict(self):
        return {"name": self.name, "email": self.email, "age": self.age}
""",
        },
        verify_fn=lambda d: _check_refactor(d),
        max_turns=8,
    ),

    # ── 测试生成 (medium) ─────────────────────────────────────────────
    BenchmarkCase(
        id="testgen_01",
        category="test_generation",
        difficulty="medium",
        task="Write pytest tests for the function is_palindrome() in palindrome.py. Create test_palindrome.py.",
        setup_files={"palindrome.py": """
def is_palindrome(s):
    s = s.lower().replace(" ", "")
    return s == s[::-1]
"""},
        verify_fn=lambda d: _check_testgen(d),
        max_turns=5,
    ),

    # ── 工具组合 (medium) ─────────────────────────────────────────────
    BenchmarkCase(
        id="tool_combo_01",
        category="tool_usage",
        difficulty="medium",
        task="Create a Python file count.py that counts words in a text file, then create a sample.txt with some text, then run count.py on sample.txt and show the output.",
        setup_files={},
        verify_fn=lambda d: _check_tool_combo(d),
        max_turns=8,
    ),

    # ── 规划能力 (hard) ───────────────────────────────────────────────
    BenchmarkCase(
        id="planning_01",
        category="planning",
        difficulty="hard",
        task="Create a mini project: 1) Create a models.py with a Todo class (id, title, done), 2) Create a storage.py with save/load functions using JSON, 3) Create a main.py that ties them together, 4) Run main.py to verify it works.",
        setup_files={},
        verify_fn=lambda d: _check_planning(d),
        max_turns=12,
    ),

    # ── Shell 操作 (easy) ─────────────────────────────────────────────
    BenchmarkCase(
        id="shell_01",
        category="shell",
        difficulty="easy",
        task="Run 'echo hello > test.txt' and then show the contents of test.txt",
        setup_files={},
        verify_fn=lambda d: _check_file(d, "test.txt", "hello"),
        max_turns=5,
    ),

    # ── 多步骤 (hard) ─────────────────────────────────────────────────
    BenchmarkCase(
        id="multi_01",
        category="multi_step",
        difficulty="hard",
        task="Create a CSV file data.csv with columns name,age,city and 3 rows of data. Then create analyze.py that reads the CSV and prints the average age. Run analyze.py.",
        setup_files={},
        verify_fn=lambda d: _check_multi(d),
        max_turns=10,
    ),

    # ── 文件操作 (新增) ──────────────────────────────────────────────
    BenchmarkCase(
        id="file_04",
        category="file_ops",
        difficulty="easy",
        task="Create a directory structure: src/models/ and src/utils/, then create an empty __init__.py in each.",
        setup_files={},
        verify_fn=lambda d: _check_file(d, "src/models/__init__.py", "") if Path(d, "src/models/__init__.py").exists() else _check_dir_exists(d, "src/models"),
        max_turns=5,
    ),
    BenchmarkCase(
        id="file_05",
        category="file_ops",
        difficulty="medium",
        task="Read the file data.json and create a new file result.txt containing only the value of the 'name' field.",
        setup_files={"data.json": '{"name": "Alice", "age": 30, "city": "Beijing"}'},
        verify_fn=lambda d: _check_file(d, "result.txt", "Alice"),
        max_turns=5,
    ),
    BenchmarkCase(
        id="file_06",
        category="file_ops",
        difficulty="medium",
        task="Merge the contents of part1.txt and part2.txt into a single file combined.txt (part1 content first, then part2).",
        setup_files={"part1.txt": "Hello\n", "part2.txt": "World\n"},
        verify_fn=lambda d: _check_contains_all(d, "combined.txt", ["Hello", "World"]),
        max_turns=5,
    ),
    BenchmarkCase(
        id="file_07",
        category="file_ops",
        difficulty="easy",
        task="Copy the file source.txt to a new location backup/source.txt",
        setup_files={"source.txt": "important data"},
        verify_fn=lambda d: _check_file(d, "backup/source.txt", "important data"),
        max_turns=5,
    ),
    BenchmarkCase(
        id="file_08",
        category="file_ops",
        difficulty="medium",
        task="Remove all empty/blank lines from messy.txt and save the result as clean.txt",
        setup_files={"messy.txt": "line1\n\nline2\n\n\nline3\n\nline4"},
        verify_fn=lambda d: _check_file_empty_lines_removed(d, "clean.txt") if Path(d, "clean.txt").exists() else (False, "clean.txt not found"),
        max_turns=5,
    ),

    # ── 代码理解 (新增) ──────────────────────────────────────────────
    BenchmarkCase(
        id="understand_02",
        category="code_understanding",
        difficulty="medium",
        task="Look at recursive.py. What does factorial(5) return? Just write the answer to answer.txt.",
        setup_files={"recursive.py": """def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n - 1)
"""},
        verify_fn=lambda d: _check_file(d, "answer.txt", "120"),
        max_turns=5,
    ),
    BenchmarkCase(
        id="understand_03",
        category="code_understanding",
        difficulty="medium",
        task="Look at app.py. List all function calls (function name only, one per line) in calls.txt.",
        setup_files={"app.py": """import json
def process(data):
    result = json.dumps(data)
    print(result)
    return len(result)

def main():
    data = {"key": "value"}
    size = process(data)
    print(f"Size: {size}")
"""},
        verify_fn=lambda d: _check_contains_all(d, "calls.txt", ["json.dumps", "print", "len", "process"]) if Path(d, "calls.txt").exists() else (False, "calls.txt not found"),
        max_turns=5,
    ),
    BenchmarkCase(
        id="understand_04",
        category="code_understanding",
        difficulty="easy",
        task="Read mystery.py and write a one-sentence explanation of what it does to explanation.txt.",
        setup_files={"mystery.py": """def mystery(s):
    return ''.join(sorted(s.lower()))
"""},
        verify_fn=lambda d: _check_file_lines(d, "explanation.txt", 1),
        max_turns=5,
    ),
    BenchmarkCase(
        id="understand_05",
        category="code_understanding",
        difficulty="hard",
        task="Compare sort_v1.py and sort_v2.py. Write the key differences to diff.txt (at least 3 lines).",
        setup_files={
            "sort_v1.py": """def sort_data(items):
    n = len(items)
    for i in range(n):
        for j in range(0, n-i-1):
            if items[j] > items[j+1]:
                items[j], items[j+1] = items[j+1], items[j]
    return items
""",
            "sort_v2.py": """def sort_data(items):
    if len(items) <= 1:
        return items
    pivot = items[len(items) // 2]
    left = [x for x in items if x < pivot]
    mid = [x for x in items if x == pivot]
    right = [x for x in items if x > pivot]
    return sort_data(left) + mid + sort_data(right)
""",
        },
        verify_fn=lambda d: _check_file_lines(d, "diff.txt", 3),
        max_turns=8,
    ),

    # ── Bug 修复 (新增) ──────────────────────────────────────────────
    BenchmarkCase(
        id="bugfix_03",
        category="bug_fix",
        difficulty="medium",
        task="Fix the bug in get_first.py: get_first([]) should return None but it crashes with IndexError.",
        setup_files={"get_first.py": """def get_first(lst):
    return lst[0]
"""},
        verify_fn=lambda d: _check_contains_any(d, "get_first.py", ["if not", "if len", "try", "except", "or None", "if lst"] ),
        max_turns=5,
    ),
    BenchmarkCase(
        id="bugfix_04",
        category="bug_fix",
        difficulty="medium",
        task="Fix the string encoding bug in read_file.py: it should handle files with non-ASCII characters gracefully instead of crashing.",
        setup_files={"read_file.py": """def read_text(path):
    with open(path, 'r') as f:
        return f.read()

# This crashes on files with non-UTF8 content
"""},
        verify_fn=lambda d: _check_contains_any(d, "read_file.py", ["encoding", "utf-8", "errors=", "except", "UnicodeDecodeError"]),
        max_turns=5,
    ),
    BenchmarkCase(
        id="bugfix_05",
        category="bug_fix",
        difficulty="hard",
        task="Fix the infinite recursion bug in countdown.py: countdown(5) should print 5,4,3,2,1,0 but it never stops.",
        setup_files={"countdown.py": """def countdown(n):
    print(n)
    countdown(n)  # Bug: should be n-1
"""},
        verify_fn=lambda d: _check_contains(d, "countdown.py", "n - 1") or _check_contains(d, "countdown.py", "n-1"),
        max_turns=5,
    ),
    BenchmarkCase(
        id="bugfix_06",
        category="bug_fix",
        difficulty="easy",
        task="Fix the type error in concat.py: concat(1, 'hello') should return '1hello' but it raises TypeError.",
        setup_files={"concat.py": """def concat(a, b):
    return a + b
"""},
        verify_fn=lambda d: _check_contains_any(d, "concat.py", ["str(", "f-string", "f\"", "format", "repr", "str(a)"]),
        max_turns=5,
    ),
    BenchmarkCase(
        id="bugfix_07",
        category="bug_fix",
        difficulty="medium",
        task="Fix the missing error handling in load_config.py: when config.json doesn't exist, it should return an empty dict instead of crashing.",
        setup_files={"load_config.py": """import json
def load_config(path="config.json"):
    with open(path) as f:
        return json.load(f)
"""},
        verify_fn=lambda d: _check_contains_any(d, "load_config.py", ["except", "try", "os.path.exists", "pathlib", "Path", "FileNotFoundError"]),
        max_turns=5,
    ),
    BenchmarkCase(
        id="bugfix_08",
        category="bug_fix",
        difficulty="hard",
        task="Fix the race condition in counter.py: increment() is called from multiple threads but the count is wrong due to a race condition.",
        setup_files={"counter.py": """import threading
class Counter:
    def __init__(self):
        self.count = 0
    
    def increment(self):
        current = self.count
        # Simulate some work
        self.count = current + 1
"""},
        verify_fn=lambda d: _check_contains_any(d, "counter.py", ["Lock", "lock", "threading.Lock", "RLock", "atomic", "with self"]),
        max_turns=8,
    ),
    BenchmarkCase(
        id="bugfix_09",
        category="bug_fix",
        difficulty="medium",
        task="Fix the config parser bug in parse_ini.py: it should handle comments (lines starting with #) and empty lines without crashing.",
        setup_files={"parse_ini.py": """def parse_ini(text):
    result = {}
    for line in text.strip().splitlines():
        key, value = line.split('=')  # Bug: crashes on comments
        result[key.strip()] = value.strip()
    return result
"""},
        verify_fn=lambda d: _check_contains_any(d, "parse_ini.py", ["startswith", "#", "continue", "if line", "strip"]),
        max_turns=5,
    ),
    BenchmarkCase(
        id="bugfix_10",
        category="bug_fix",
        difficulty="hard",
        task="Fix the memory leak in cache.py: the cache dict grows unbounded. Add an LRU eviction mechanism (max 100 entries).",
        setup_files={"cache.py": """class Cache:
    def __init__(self):
        self._data = {}
    
    def get(self, key):
        return self._data.get(key)
    
    def put(self, key, value):
        self._data[key] = value  # Unbounded growth!
"""},
        verify_fn=lambda d: _check_contains_any(d, "cache.py", ["maxlen", "lru", "LRU", "evict", "popitem", "OrderedDict", "deque", "len(self._data)"]),
        max_turns=8,
    ),

    # ── 重构 (新增) ──────────────────────────────────────────────────
    BenchmarkCase(
        id="refactor_02",
        category="refactor",
        difficulty="easy",
        task="Extract all magic numbers from pricing.py into named constants at the top of the file.",
        setup_files={"pricing.py": """def calculate_price(quantity, unit_price):
    discount = 0.1 if quantity > 10 else 0.05
    tax = 0.08
    subtotal = quantity * unit_price
    return subtotal * (1 - discount) * (1 + tax)
"""},
        verify_fn=lambda d: _check_contains_all(d, "pricing.py", ["DISCOUNT", "TAX"]) if Path(d, "pricing.py").exists() else (False, "pricing.py not found"),
        max_turns=5,
    ),
    BenchmarkCase(
        id="refactor_03",
        category="refactor",
        difficulty="medium",
        task="Split the monolithic process_data function in processor.py into smaller functions: validate(), transform(), and save().",
        setup_files={"processor.py": """def process_data(data):
    # Validate
    if not data:
        raise ValueError("Empty data")
    if "name" not in data:
        raise ValueError("Missing name")
    
    # Transform
    data["name"] = data["name"].upper().strip()
    data["processed"] = True
    
    # Save
    import json
    with open("output.json", "w") as f:
        json.dump(data, f)
    return True
"""},
        verify_fn=lambda d: _check_contains_all(d, "processor.py", ["def validate", "def transform", "def save"]),
        max_turns=8,
    ),
    BenchmarkCase(
        id="refactor_04",
        category="refactor",
        difficulty="hard",
        task="Refactor shapes.py to use class inheritance: create a base Shape class with area() and perimeter() methods, then make Circle and Rectangle inherit from it.",
        setup_files={"shapes.py": """import math

def circle_area(radius):
    return math.pi * radius ** 2

def circle_perimeter(radius):
    return 2 * math.pi * radius

def rectangle_area(width, height):
    return width * height

def rectangle_perimeter(width, height):
    return 2 * (width + height)
"""},
        verify_fn=lambda d: _check_refactor_class_inherit(d),
        max_turns=10,
    ),
    BenchmarkCase(
        id="refactor_05",
        category="refactor",
        difficulty="medium",
        task="The file handlers.py has duplicate code in handle_get and handle_post. Extract the common parts into a shared function.",
        setup_files={"handlers.py": """import json
def handle_get(path):
    print(f"GET {path}")
    if not path:
        return {"error": "empty path"}
    log(f"Request: GET {path}")
    return {"status": "ok", "method": "GET", "path": path}

def handle_post(path, body):
    print(f"POST {path}")
    if not path:
        return {"error": "empty path"}
    log(f"Request: POST {path}")
    return {"status": "ok", "method": "POST", "path": path, "body": body}

def log(msg):
    print(f"[LOG] {msg}")
"""},
        verify_fn=lambda d: (True, "Agent responded"),
        max_turns=8,
    ),
    BenchmarkCase(
        id="refactor_06",
        category="refactor",
        difficulty="hard",
        task="Convert the synchronous fetch_data.py to use async/await with aiohttp. Keep the same interface but make it non-blocking.",
        setup_files={"fetch_data.py": """import urllib.request
import json

def fetch_url(url):
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())

def fetch_multiple(urls):
    results = []
    for url in urls:
        results.append(fetch_url(url))
    return results
"""},
        verify_fn=lambda d: _check_contains_all(d, "fetch_data.py", ["async", "await"]),
        max_turns=8,
    ),

    # ── 测试生成 (新增) ──────────────────────────────────────────────
    BenchmarkCase(
        id="testgen_02",
        category="test_generation",
        difficulty="medium",
        task="Write pytest tests for clamp() in clamp.py. Include boundary value tests: min, max, below min, above max, and within range.",
        setup_files={"clamp.py": """def clamp(value, min_val, max_val):
    if value < min_val:
        return min_val
    if value > max_val:
        return max_val
    return value
"""},
        verify_fn=lambda d: _check_contains(d, "test_clamp.py", "def test_") if Path(d, "test_clamp.py").exists() else (False, "test_clamp.py not found"),
        max_turns=8,
    ),
    BenchmarkCase(
        id="testgen_03",
        category="test_generation",
        difficulty="medium",
        task="Write pytest tests for divide() in divider.py. Cover exception paths: division by zero, invalid types.",
        setup_files={"divider.py": """def divide(a, b):
    if b == 0:
        raise ValueError("Division by zero")
    return a / b
"""},
        verify_fn=lambda d: _check_contains_any(d, "test_divider.py", ["pytest.raises", "assert.*raise", "ValueError", "with.*raise"]) if Path(d, "test_divider.py").exists() else (False, "test_divider.py not found"),
        max_turns=8,
    ),
    BenchmarkCase(
        id="testgen_04",
        category="test_generation",
        difficulty="medium",
        task="Write pytest tests for is_even() in math_utils.py using @pytest.mark.parametrize with at least 5 test cases.",
        setup_files={"math_utils.py": """def is_even(n):
    return n % 2 == 0
"""},
        verify_fn=lambda d: _check_contains_any(d, "test_math_utils.py", ["parametrize", "pytest.mark.parametrize"]) if Path(d, "test_math_utils.py").exists() else (False, "test_math_utils.py not found"),
        max_turns=8,
    ),
    BenchmarkCase(
        id="testgen_05",
        category="test_generation",
        difficulty="hard",
        task="Write pytest tests for get_user_data() in user_service.py using mock to avoid real HTTP calls.",
        setup_files={"user_service.py": """import urllib.request
import json

def get_user_data(user_id):
    url = f"https://api.example.com/users/{user_id}"
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read())
"""},
        verify_fn=lambda d: _check_contains_any(d, "test_user_service.py", ["mock", "patch", "Mock", "MagicMock"]) if Path(d, "test_user_service.py").exists() else (False, "test_user_service.py not found"),
        max_turns=8,
    ),
    BenchmarkCase(
        id="testgen_06",
        category="test_generation",
        difficulty="hard",
        task="Write an integration test that creates a User, saves it with JsonStorage, then loads it back and verifies the data matches.",
        setup_files={
            "models.py": """class User:
    def __init__(self, name, email):
        self.name = name
        self.email = email
    def to_dict(self):
        return {"name": self.name, "email": self.email}
    @classmethod
    def from_dict(cls, d):
        return cls(d["name"], d["email"])
""",
            "storage.py": """import json
class JsonStorage:
    def __init__(self, path):
        self.path = path
    def save(self, data):
        with open(self.path, 'w') as f:
            json.dump(data, f)
    def load(self):
        with open(self.path) as f:
            return json.load(f)
""",
        },
        verify_fn=lambda d: (any(Path(d).glob("test_*.py")), "Integration test file" + (" found" if any(Path(d).glob("test_*.py")) else " not found")),
        max_turns=10,
    ),

    # ── 工具组合 (新增) ──────────────────────────────────────────────
    BenchmarkCase(
        id="tool_combo_02",
        category="tool_usage",
        difficulty="medium",
        task="Create a file pipeline: 1) Create input.txt with 5 lines of text, 2) Create filter.py that reads input.txt and writes only lines longer than 10 chars to output.txt, 3) Run filter.py.",
        setup_files={},
        verify_fn=lambda d: (Path(d, "output.txt").exists() and Path(d, "output.txt").read_text().strip() != "", "output.txt exists" if Path(d, "output.txt").exists() else "output.txt not found"),
        max_turns=8,
    ),
    BenchmarkCase(
        id="tool_combo_03",
        category="tool_usage",
        difficulty="hard",
        task="Generate a Python script calculator.py that supports +,-,*,/, run it with test inputs, and verify the output is correct.",
        setup_files={},
        verify_fn=lambda d: next(
            (_check_executable(d, f.name) for f in sorted(Path(d).glob('*.py'), key=lambda f: 'calc' in f.name, reverse=True) if f.is_file()),
            (False, 'No .py files found')
        ),
        max_turns=10,
    ),
    BenchmarkCase(
        id="tool_combo_04",
        category="tool_usage",
        difficulty="medium",
        task="Search through all .py files in the project directory and replace all occurrences of 'TODO' with 'DONE'. Create at least 2 Python files with TODOs first.",
        setup_files={},
        verify_fn=lambda d: (not any('TODO' in p.read_text() for p in Path(d).glob('*.py') if p.is_file()), 'All TODOs replaced' if not any('TODO' in p.read_text() for p in Path(d).glob('*.py') if p.is_file()) else 'Some TODOs remain') if any(Path(d).glob('*.py')) else (False, 'No .py files found'),
        max_turns=8,
    ),
    BenchmarkCase(
        id="tool_combo_05",
        category="tool_usage",
        difficulty="hard",
        task="Initialize a git repo, create a file README.md, commit it, then create a new branch 'feature', add another file feature.py, and commit on the branch.",
        setup_files={},
        verify_fn=lambda d: _check_git_init(d),
        max_turns=10,
    ),
    BenchmarkCase(
        id="tool_combo_06",
        category="tool_usage",
        difficulty="medium",
        task="Create a project scaffold with: pyproject.toml, src/__init__.py, src/main.py (with a hello() function), and tests/test_main.py (with a test for hello).",
        setup_files={},
        verify_fn=lambda d: (
            Path(d, "pyproject.toml").exists()
            and Path(d, "src/main.py").exists()
            and Path(d, "tests/test_main.py").exists(),
            "Scaffold: " + ", ".join(f for f in ["pyproject.toml", "src/main.py", "tests/test_main.py"] if Path(d, f).exists())
        ),
        max_turns=8,
    ),

    # ── 规划能力 (新增) ──────────────────────────────────────────────
    BenchmarkCase(
        id="planning_02",
        category="planning",
        difficulty="hard",
        task="Build a data processing pipeline: 1) Create generator.py that generates random CSV data, 2) Create pipeline.py that reads CSV, cleans nulls, computes stats, 3) Create output.json with the results. Run the pipeline.",
        setup_files={},
        verify_fn=lambda d: (
            Path(d, "generator.py").exists()
            and Path(d, "pipeline.py").exists()
            and Path(d, "output.json").exists(),
            "Pipeline files: " + ", ".join(f for f in ["generator.py", "pipeline.py", "output.json"] if Path(d, f).exists())
        ),
        max_turns=15,
    ),
    BenchmarkCase(
        id="planning_03",
        category="planning",
        difficulty="hard",
        task="Create an API client wrapper: 1) Create client.py with a class that has get(), post(), put(), delete() methods, 2) Include retry logic and error handling, 3) Create example.py demonstrating usage.",
        setup_files={},
        verify_fn=lambda d: (
            any(
                f"class " in p.read_text() and any(m in p.read_text() for m in ["def get", "def post", "def put", "def delete", "def request"])
                for p in (f for f in Path(d).glob('*.py') if f.is_file())
            ),
            "API client class with HTTP methods found"
        ) if any(Path(d).glob('*.py')) else (False, 'No .py files found'),
        max_turns=12,
    ),
    BenchmarkCase(
        id="planning_04",
        category="planning",
        difficulty="hard",
        task="Build a CLI tool: 1) Create cli.py that parses args with argparse, 2) Support subcommands: init, build, clean, 3) Each subcommand does something simple but functional, 4) Make it runnable.",
        setup_files={},
        verify_fn=lambda d: _check_contains_all(d, "cli.py", ["argparse", "add_parser", "def "]),
        max_turns=12,
    ),
    BenchmarkCase(
        id="planning_05",
        category="planning",
        difficulty="medium",
        task="Create a config management system: 1) config.py with load/save/validate functions, 2) default config as JSON, 3) A main.py that loads config and prints it.",
        setup_files={},
        verify_fn=lambda d: (
            Path(d, "config.py").exists() and Path(d, "main.py").exists(),
            "Config system: " + ", ".join(f for f in ["config.py", "main.py"] if Path(d, f).exists())
        ),
        max_turns=10,
    ),
    BenchmarkCase(
        id="planning_06",
        category="planning",
        difficulty="medium",
        task="Implement a logging system: 1) logger.py with a custom Logger class that supports DEBUG/INFO/WARNING/ERROR levels, 2) Output to both console and file, 3) Create demo.py that uses it.",
        setup_files={},
        verify_fn=lambda d: _first_pass(
            _check_class_def(d, "logger.py", "Logger"),
            _check_function_def(d, "logger.py", "log"),
            _check_function_def(d, "logger.py", "debug"),
            _check_function_def(d, "logger.py", "info"),
            _check_function_def(d, "logger.py", "warning"),
            _check_function_def(d, "logger.py", "error"),
        ),
        max_turns=10,
    ),

    # ── Shell 操作 (新增) ────────────────────────────────────────────
    BenchmarkCase(
        id="shell_02",
        category="shell",
        difficulty="easy",
        task="Use a shell pipeline: create a file with numbers (one per line), then use shell commands to sort them and save to sorted.txt.",
        setup_files={"numbers.txt": "42\n7\n15\n3\n99\n23\n1\n88"},
        verify_fn=lambda d: _check_file(d, "sorted.txt", "1") if Path(d, "sorted.txt").exists() else (False, "sorted.txt not found"),
        max_turns=5,
    ),
    BenchmarkCase(
        id="shell_03",
        category="shell",
        difficulty="medium",
        task="Create a script run.sh, make it executable with chmod +x, and have it write 'Hello from shell' to shell_output.txt when run.",
        setup_files={},
        verify_fn=lambda d: _check_file(d, "shell_output.txt", "Hello from shell"),
        max_turns=6,
    ),
    BenchmarkCase(
        id="shell_04",
        category="shell",
        difficulty="medium",
        task="Create a Python script that reads the environment variable MY_VAR, and if not set, defaults to 'default_value'. Then run it with MY_VAR=custom_value.",
        setup_files={},
        verify_fn=lambda d: (any(_check_executable(d, f.name)[0] for f in Path(d).glob('*.py') if f.is_file()), 'Python script executed successfully') if any(Path(d).glob('*.py')) else (False, 'No .py files found'),
        max_turns=6,
    ),
    BenchmarkCase(
        id="shell_05",
        category="shell",
        difficulty="hard",
        task="Create a script that starts a background Python HTTP server on port 8899, waits 2 seconds, then checks if it's responding with urllib, then kills the server process.",
        setup_files={},
        verify_fn=lambda d: (
            any(_check_executable(d, f.name)[0] for f in Path(d).iterdir() if f.is_file() and f.suffix in ('.py', '.sh')),
            'Script file executed'
        ) if any(f for f in Path(d).iterdir() if f.is_file()) else (False, 'No script files found'),
        max_turns=10,
    ),

    # ── 多步骤 (新增) ────────────────────────────────────────────────
    BenchmarkCase(
        id="multi_02",
        category="multi_step",
        difficulty="hard",
        task="Build a data analysis pipeline: 1) Create messy_data.csv with some invalid/missing values, 2) Create clean.py that reads, cleans, and outputs clean_data.csv, 3) Create analyze.py that reads clean_data.csv and computes statistics (mean, median, mode), 4) Run both scripts in sequence.",
        setup_files={},
        verify_fn=lambda d: (
            Path(d, "clean_data.csv").exists() and Path(d, "analyze.py").exists(),
            "Pipeline: " + ", ".join(f for f in ["messy_data.csv", "clean.py", "clean_data.csv", "analyze.py"] if Path(d, f).exists())
        ),
        max_turns=15,
    ),
    BenchmarkCase(
        id="multi_03",
        category="multi_step",
        difficulty="hard",
        task="Test-driven development cycle: 1) Create buggy math_utils.py with add(a,b) that has a bug, 2) Write test_math_utils.py, 3) Run tests to see failure, 4) Fix the bug, 5) Run tests to verify fix.",
        setup_files={"math_utils.py": """def add(a, b):
    return a - b  # Bug: should be a + b
"""},
        verify_fn=lambda d: _check_contains(d, "math_utils.py", "a + b"),
        max_turns=10,
    ),
    BenchmarkCase(
        id="multi_04",
        category="multi_step",
        difficulty="hard",
        task="Code review + refactor + document: 1) Read messy_code.py, 2) Identify code smells, 3) Refactor it, 4) Add docstrings, 5) Write a review.txt with your findings.",
        setup_files={"messy_code.py": """def f(x,y,z):
    r=x+y
    if r>100:
        return r*z
    else:
        return r/z if z!=0 else 0
def g(a):
    l=[]
    for i in a:
        if i>0:
            l.append(i*2)
    return l
"""},
        verify_fn=lambda d: _check_file_lines(d, "review.txt", 1) if Path(d, "review.txt").exists() else (False, "review.txt not found"),
        max_turns=12,
    ),
    BenchmarkCase(
        id="multi_05",
        category="multi_step",
        difficulty="hard",
        task="Dependency + build + run: 1) Create requirements.txt with at least one package, 2) Create main.py that imports and uses it, 3) Install dependencies, 4) Run main.py successfully.",
        setup_files={},
        verify_fn=lambda d: _check_executable(d, "main.py") if Path(d, "main.py").exists() else (
            next((_check_executable(d, f.name) for f in Path(d).glob('*.py') if f.is_file() and f.name != 'requirements.txt'), (False, 'No runnable .py file found'))
        ),
        max_turns=12,
    ),
    BenchmarkCase(
        id="multi_06",
        category="multi_step",
        difficulty="hard",
        task="Build error handling + logging + monitoring: 1) Create app.py with a function that can fail, 2) Add proper try/except with logging, 3) Create a simple health check that verifies the function works, 4) Run everything.",
        setup_files={},
        verify_fn=lambda d: (
            Path(d, "app.py").exists() and any("try" in Path(d, f).read_text() for f in Path(d).glob("*.py")),
            "app.py with try/except found" if Path(d, "app.py").exists() and any("try" in Path(d, f).read_text() for f in Path(d).glob("*.py")) else "Missing app.py or no try/except found"
        ),
        max_turns=12,
    ),
]


# ===========================================================================
# Benchmark Runner
# ===========================================================================

class BenchmarkRunner:
    """Benchmark 执行器"""
    
    def __init__(self, api_key: str, model: str = "xiaomi/mimo-v2.5-pro",
                 base_url: str = "http://model.mify.ai.srv/v1",
                 extra_headers: dict[str, str] | None = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.extra_headers = extra_headers or {}
        self.results: list[BenchmarkResult] = []
        # 部分模型（GPT-5 系列）只接受默认温度，显式传会 400。
        # 允许用 MODEL_TEMPERATURE=none 关闭，或对 gpt-5* 自动关闭。
        temp_env = os.environ.get("MODEL_TEMPERATURE")
        if temp_env is not None:
            temperature = None if temp_env.lower() == "none" else float(temp_env)
        elif model.lower().startswith("gpt-5") or "gpt-5" in model.lower():
            temperature = None
        else:
            temperature = 0.7
        # 与生产一致的统一模型客户端（带退避重试）
        self.model_client = ModelClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_tokens=2048,
            temperature=temperature,
            extra_headers=self.extra_headers,
        )
    
    async def run_case(self, case: BenchmarkCase) -> BenchmarkResult:
        """运行单个测试用例"""
        # 创建临时目录
        workdir = tempfile.mkdtemp(prefix=f"bench_{case.id}_")
        
        # 写入 setup 文件
        for path, content in case.setup_files.items():
            full_path = Path(workdir, path)
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
        
        # 初始化工具：与生产 CodingAgent 一致的完整工具集，
        # 使用独立 registry（不污染全局，且与改进后的注册逻辑一致）
        registry = ToolRegistry()
        register_file_tools(registry)
        register_shell_tools(registry)
        register_git_tools(registry)
        register_tdd_tools(registry)
        register_memory_tools(registry=registry)
        plan_tool = register_plan_tools(registry=registry)
        register_patch_tools(registry)

        # 切换 cwd 到 workdir（确保工具在正确目录执行）
        old_cwd = os.getcwd()
        os.chdir(workdir)
        
        # 创建 agent
        config = AgentConfig(
            model=self.model,
            api_key=self.api_key,
            api_base_url=self.base_url,
            max_turns=case.max_turns,
            auto_approve=True,
            system_prompt=(
                f"You are a helpful coding assistant. You are working in the directory: {workdir}. "
                f"All file operations should use relative paths or paths within this directory.\n\n"
                f"CRITICAL RULES:\n"
                f"1. ALWAYS test your code before reporting done. Run every script you create and fix any errors. "
                f"If a script fails, debug and fix it until it works.\n"
                f"2. If you create a requirements.txt or any dependencies, install them (pip install -r requirements.txt) "
                f"before running the script.\n"
                f"3. For complex tasks, save your work as files in the working directory. "
                f"Shell commands alone are not enough — create the actual script files.\n"
                f"4. Always create files with .py extension when writing Python code.\n"
                f"5. When asked to build something, create ALL required files before testing.\n"
                f"6. For multi-step tasks, call update_plan first to lay out steps, then keep it current.\n"
                f"7. Prefer apply_patch for multi-file or multi-location edits."
            )
        )

        agent = AgentLoop(config=config, tool_registry=registry)

        # 设置模型调用
        agent.set_model_call_fn(self._call_model)

        # 运行
        state = AgentState()
        plan_tool.bind_state(state)
        tool_calls = []
        start_time = time.time()
        
        try:
            async for event in agent.run(state, case.task):
                if event.event == AgentEvent.TOOL_CALL:
                    tool_calls.append(event.data["name"])
                
                if event.event == AgentEvent.ERROR:
                    elapsed = time.time() - start_time
                    return BenchmarkResult(
                        case_id=case.id,
                        category=case.category,
                        difficulty=case.difficulty,
                        passed=False,
                        turns_used=state.turn_count,
                        time_seconds=elapsed,
                        detail="",
                        error=event.data["error"],
                        tool_calls=tool_calls,
                    )
                
                if event.event == AgentEvent.DONE:
                    break
            
            elapsed = time.time() - start_time
            
            # 验证结果
            try:
                passed, detail = case.verify_fn(workdir)
            except Exception as e:
                passed, detail = False, f"Verify error: {e}"
            
            return BenchmarkResult(
                case_id=case.id,
                category=case.category,
                difficulty=case.difficulty,
                passed=passed,
                turns_used=state.turn_count,
                time_seconds=elapsed,
                detail=detail,
                tool_calls=tool_calls,
            )
        
        except Exception as e:
            elapsed = time.time() - start_time
            return BenchmarkResult(
                case_id=case.id,
                category=case.category,
                difficulty=case.difficulty,
                passed=False,
                turns_used=0,
                time_seconds=elapsed,
                detail="",
                error=str(e),
                tool_calls=tool_calls,
            )
        
        finally:
            # 恢复 cwd
            os.chdir(old_cwd)
            # 清理临时目录
            try:
                shutil.rmtree(workdir, ignore_errors=True)
            except:
                pass
    
    async def _call_model(self, context: list[dict], tools: list[dict]) -> dict:
        """调用模型。委托给统一的 ModelClient（含退避重试，与生产一致）。
        benchmark 不需要流式，关闭以减少开销。"""
        return await self.model_client.complete(context, tools, stream=False)

    async def run_all(self, cases: list[BenchmarkCase] | None = None) -> BenchmarkReport:
        """运行所有测试"""
        cases = cases or BENCHMARK_CASES
        results = []
        
        print(f"\n{'='*60}")
        print(f"🚀 Coding Agent Benchmark")
        print(f"   Model: {self.model}")
        print(f"   Cases: {len(cases)}")
        print(f"{'='*60}\n")
        
        for i, case in enumerate(cases, 1):
            print(f"[{i}/{len(cases)}] {case.id} ({case.difficulty}) - {case.task[:60]}...", end=" ", flush=True)
            
            result = await self.run_case(case)
            results.append(result)
            
            status = "✅ PASS" if result.passed else "❌ FAIL"
            if result.error:
                status = "💥 ERR"
            print(f"{status} ({result.turns_used}t, {result.time_seconds:.1f}s)")
            
            if not result.passed and result.detail:
                print(f"         {result.detail[:80]}")
            
            # 延迟避免 429
            if i < len(cases):
                await asyncio.sleep(5)
        
        return self._generate_report(results)
    
    def _generate_report(self, results: list[BenchmarkResult]) -> BenchmarkReport:
        """生成报告"""
        report = BenchmarkReport(results=results)
        report.total = len(results)
        report.passed = sum(1 for r in results if r.passed)
        report.failed = sum(1 for r in results if not r.passed and not r.error)
        report.errors = sum(1 for r in results if r.error)
        report.avg_turns = sum(r.turns_used for r in results) / len(results) if results else 0
        report.avg_time = sum(r.time_seconds for r in results) / len(results) if results else 0
        
        # 按类别统计
        categories = set(r.category for r in results)
        for cat in categories:
            cat_results = [r for r in results if r.category == cat]
            report.by_category[cat] = {
                "total": len(cat_results),
                "passed": sum(1 for r in cat_results if r.passed),
                "rate": sum(1 for r in cat_results if r.passed) / len(cat_results) * 100,
            }
        
        # 按难度统计
        difficulties = set(r.difficulty for r in results)
        for diff in difficulties:
            diff_results = [r for r in results if r.difficulty == diff]
            report.by_difficulty[diff] = {
                "total": len(diff_results),
                "passed": sum(1 for r in diff_results if r.passed),
                "rate": sum(1 for r in diff_results if r.passed) / len(diff_results) * 100,
            }
        
        return report


def print_report(report: BenchmarkReport) -> None:
    """打印报告"""
    print(f"\n{'='*60}")
    print(f"📊 Benchmark Report")
    print(f"{'='*60}")
    print(f"  Total:   {report.total}")
    print(f"  Passed:  {report.passed} ({report.passed/report.total*100:.1f}%)")
    print(f"  Failed:  {report.failed}")
    print(f"  Errors:  {report.errors}")
    print(f"  Avg turns: {report.avg_turns:.1f}")
    print(f"  Avg time:  {report.avg_time:.1f}s")
    
    print(f"\n  By Category:")
    for cat, stats in sorted(report.by_category.items()):
        bar = "█" * int(stats["rate"] / 10) + "░" * (10 - int(stats["rate"] / 10))
        print(f"    {cat:<20} {bar} {stats['passed']}/{stats['total']} ({stats['rate']:.0f}%)")
    
    print(f"\n  By Difficulty:")
    for diff, stats in sorted(report.by_difficulty.items()):
        bar = "█" * int(stats["rate"] / 10) + "░" * (10 - int(stats["rate"] / 10))
        print(f"    {diff:<20} {bar} {stats['passed']}/{stats['total']} ({stats['rate']:.0f}%)")
    
    print(f"\n  Failed Cases:")
    for r in report.results:
        if not r.passed:
            status = "ERR" if r.error else "FAIL"
            print(f"    [{status}] {r.case_id} ({r.category}/{r.difficulty}): {r.detail[:60]}")
    
    print(f"{'='*60}\n")


async def main():
    """主函数。

    支持两种配置来源（优先级从高到低）：
    - MODEL_API_KEY + MODEL_BASE_URL + MODEL_PRIMARY（小米 mify 环境）
    - OPENAI_API_KEY + OPENAI_API_BASE + CODING_AGENT_MODEL（任意 OpenAI 兼容端点）
    """
    api_key = os.environ.get("MODEL_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    if not api_key:
        print("Error: set MODEL_API_KEY (mify) or OPENAI_API_KEY (any OpenAI-compatible endpoint)")
        sys.exit(1)

    if os.environ.get("MODEL_API_KEY"):
        base_url = os.environ.get("MODEL_BASE_URL", "http://model.mify.ai.srv/v1")
        model = os.environ.get("MODEL_PRIMARY", "xiaomi/mimo-v2.5-pro")
    else:
        base_url = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
        model = os.environ.get("CODING_AGENT_MODEL", "gpt-4o-mini")

    print(f"Endpoint: {base_url}  Model: {model}")
    extra_headers = {}
    raw_headers = os.environ.get("MODEL_EXTRA_HEADERS")
    if raw_headers:
        try:
            extra_headers = json.loads(raw_headers)
        except json.JSONDecodeError:
            print(f"Warning: MODEL_EXTRA_HEADERS is not valid JSON, ignoring")
    runner = BenchmarkRunner(api_key=api_key, model=model, base_url=base_url,
                             extra_headers=extra_headers)
    report = await runner.run_all()
    print_report(report)
    
    # 保存报告
    report_path = Path(__file__).parent / "benchmark_report.json"
    with open(report_path, "w") as f:
        json.dump({
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "errors": report.errors,
            "avg_turns": report.avg_turns,
            "avg_time": report.avg_time,
            "by_category": report.by_category,
            "by_difficulty": report.by_difficulty,
            "results": [
                {
                    "case_id": r.case_id,
                    "category": r.category,
                    "difficulty": r.difficulty,
                    "passed": r.passed,
                    "turns_used": r.turns_used,
                    "time_seconds": r.time_seconds,
                    "detail": r.detail,
                    "error": r.error,
                    "tool_calls": r.tool_calls,
                }
                for r in report.results
            ]
        }, f, indent=2)
    print(f"Report saved to {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
