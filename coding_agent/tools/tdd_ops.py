"""
TDD 闭环工具

提供测试驱动开发工作流：
- tdd_run_tests: 运行项目测试，返回结构化结果
- tdd_fix_loop: 自动修复失败测试（高级工具，修改代码）
- tdd_watch: 监听文件变化自动运行测试
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import Tool, ToolPermission, ToolExecutionError


# ---------------------------------------------------------------------------
# 框架检测
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    """结构化测试结果"""
    passed: int = 0
    failed: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    raw_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "raw_output": self.raw_output,
        }


class FrameworkDetector:
    """自动检测项目使用的测试框架"""

    @staticmethod
    def detect_language(root: Path) -> str | None:
        """根据项目文件推断语言"""
        markers = {
            "python": ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"],
            "javascript": ["package.json", "tsconfig.json"],
            "go": ["go.mod"],
        }
        for lang, files in markers.items():
            for f in files:
                if (root / f).exists():
                    return lang
        return None

    @staticmethod
    def detect_framework(root: Path, language: str | None = None) -> str:
        """检测具体测试框架，返回 runner 命令"""
        lang = language or FrameworkDetector.detect_language(root)

        if lang == "python":
            # 优先级：pytest > unittest
            if (root / "pytest.ini").exists() or (root / "pyproject.toml").exists():
                try:
                    pyproject = (root / "pyproject.toml").read_text()
                    if "pytest" in pyproject:
                        return "pytest"
                except Exception:
                    pass
            # 检查 conftest.py 或 test_*.py 文件
            if list(root.rglob("conftest.py")) or list(root.rglob("test_*.py")):
                return "pytest"
            # 有 tests/ 目录也算
            if (root / "tests").is_dir():
                return "pytest"
            return "pytest"  # 默认 pytest

        if lang == "javascript":
            pkg = root / "package.json"
            if pkg.exists():
                try:
                    data = json.loads(pkg.read_text())
                    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                    if "vitest" in deps:
                        return "vitest"
                    if "jest" in deps:
                        return "jest"
                    if "mocha" in deps:
                        return "mocha"
                except Exception:
                    pass
            # 默认 vitest
            return "vitest"

        if lang == "go":
            return "go test"

        return "pytest"


# ---------------------------------------------------------------------------
# Runner：执行测试并解析结果
# ---------------------------------------------------------------------------

class TestRunner:
    """执行测试并解析输出"""

    FRAMEWORK_COMMANDS = {
        "pytest": "python -m pytest {path} -v --tb=short 2>&1",
        "vitest": "npx vitest run {path} --reporter=verbose 2>&1",
        "jest": "npx jest {path} --verbose 2>&1",
        "mocha": "npx mocha {path} --recursive 2>&1",
        "go test": "go test -v {path} ./... 2>&1",
    }

    @staticmethod
    async def run_tests(
        framework: str,
        path: str | None = None,
        workdir: str | None = None,
        timeout: int = 120,
    ) -> TestResult:
        """运行测试并返回结构化结果"""
        cmd_template = TestRunner.FRAMEWORK_COMMANDS.get(framework)
        if not cmd_template:
            return TestResult(
                raw_output=f"Unsupported framework: {framework}"
            )

        cmd = cmd_template.format(path=path or ".")
        cwd = workdir or os.getcwd()

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return TestResult(raw_output=f"Tests timed out after {timeout}s")

            raw = stdout.decode("utf-8", errors="replace")
            return TestRunner._parse_output(framework, raw, proc.returncode or 0)
        except Exception as e:
            return TestResult(raw_output=f"Error running tests: {e}")

    @staticmethod
    def _parse_output(framework: str, raw: str, returncode: int) -> TestResult:
        """根据框架解析测试输出"""
        if framework == "pytest":
            return TestRunner._parse_pytest(raw)
        if framework in ("vitest", "jest", "mocha"):
            return TestRunner._parse_jest_vitest(raw)
        if framework == "go test":
            return TestRunner._parse_go_test(raw, returncode)
        # 通用：根据退出码判断
        passed = 1 if returncode == 0 else 0
        return TestResult(passed=passed, failed=0 if returncode == 0 else 1, raw_output=raw)

    @staticmethod
    def _parse_pytest(raw: str) -> TestResult:
        result = TestResult(raw_output=raw)
        # 匹配 "X passed, Y failed, Z errors"
        summary = re.search(
            r"(\d+) passed(?:.*?(\d+) failed)?(?:.*?(\d+) error)?",
            raw,
        )
        if summary:
            result.passed = int(summary.group(1))
            result.failed = int(summary.group(2) or 0)

        # 如果没有 summary 行但退出码非 0，尝试匹配 FAILURES
        if result.passed == 0 and result.failed == 0:
            fail_blocks = re.findall(r"_{4,}\s+(.*?)\s+_{4,}", raw, re.DOTALL)
            result.failed = len(fail_blocks) if fail_blocks else (1 if "FAILED" in raw else 0)

        # 解析具体错误
        for m in re.finditer(
            r"(?:FAIL|ERROR)\s+(\S+).*?\n.*?(?:AssertionError|Error):\s*(.*)",
            raw,
        ):
            result.errors.append({"file": m.group(1), "line": 0, "message": m.group(2).strip()})

        return result

    @staticmethod
    def _parse_jest_vitest(raw: str) -> TestResult:
        result = TestResult(raw_output=raw)
        # vitest / jest: "Tests: X passed, Y failed"
        summary = re.search(r"Tests:\s+(\d+) passed.*?(\d+) failed", raw)
        if summary:
            result.passed = int(summary.group(1))
            result.failed = int(summary.group(2))
        else:
            # fallback: count ✓ and ✗
            result.passed = len(re.findall(r"[✓✔]", raw))
            result.failed = len(re.findall(r"[✗✘✕]", raw))

        for m in re.finditer(r"●\s+(.*?)\n\s+(.*?):(\d+)", raw):
            result.errors.append({
                "file": m.group(2),
                "line": int(m.group(3)),
                "message": m.group(1).strip(),
            })

        return result

    @staticmethod
    def _parse_go_test(raw: str, returncode: int) -> TestResult:
        result = TestResult(raw_output=raw)
        passes = re.findall(r"--- PASS:", raw)
        fails = re.findall(r"--- FAIL:", raw)
        result.passed = len(passes)
        result.failed = len(fails)
        if result.passed == 0 and result.failed == 0 and returncode != 0:
            result.failed = 1

        for m in re.finditer(r"--- FAIL:\s+(\S+)\s+\((.*?)\)\n\s+(.*?):(\d+):\s+(.*)", raw):
            result.errors.append({
                "file": m.group(3),
                "line": int(m.group(4)),
                "message": m.group(5).strip(),
            })
        return result


# ---------------------------------------------------------------------------
# 工具 1: tdd_run_tests
# ---------------------------------------------------------------------------

class TddRunTestsTool(Tool):
    """运行项目测试，返回结构化结果"""

    @property
    def name(self) -> str:
        return "tdd_run_tests"

    @property
    def description(self) -> str:
        return (
            "Run project tests and return structured results.\n"
            "Returns: {passed, failed, errors: [{file, line, message}], raw_output}"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Test directory or file to run (optional, defaults to '.')",
                },
                "framework": {
                    "type": "string",
                    "description": "Test framework: pytest/jest/vitest/go test/auto",
                    "enum": ["pytest", "jest", "vitest", "go test", "auto"],
                    "default": "auto",
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory (optional)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 120)",
                },
            },
            "required": [],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")
        framework = kwargs.get("framework", "auto")
        workdir = kwargs.get("workdir")
        timeout = kwargs.get("timeout", 120)

        cwd = Path(workdir) if workdir else Path.cwd()

        if framework == "auto":
            framework = FrameworkDetector.detect_framework(cwd)

        result = await TestRunner.run_tests(
            framework=framework,
            path=path,
            workdir=str(cwd),
            timeout=timeout,
        )

        return json.dumps(result.to_dict(), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具 2: tdd_fix_loop
# ---------------------------------------------------------------------------

class TddFixLoopTool(Tool):
    """自动修复失败测试（高级工具，会修改代码）"""

    @property
    def name(self) -> str:
        return "tdd_fix_loop"

    @property
    def description(self) -> str:
        return (
            "Auto-fix failing tests in a loop: run tests → analyze failures → "
            "modify code → re-test → repeat until pass or limit reached.\n"
            "This tool MODIFIES source code. Use with caution."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "max_iterations": {
                    "type": "integer",
                    "description": "Max fix iterations (default: 5)",
                    "default": 5,
                },
                "test_path": {
                    "type": "string",
                    "description": "Specific test file or directory (optional)",
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory (optional)",
                },
                "framework": {
                    "type": "string",
                    "description": "Test framework (default: auto)",
                    "enum": ["pytest", "jest", "vitest", "go test", "auto"],
                    "default": "auto",
                },
            },
            "required": [],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.EXECUTE

    async def execute(self, **kwargs: Any) -> str:
        max_iterations = kwargs.get("max_iterations", 5)
        test_path = kwargs.get("test_path")
        workdir = kwargs.get("workdir")
        framework = kwargs.get("framework", "auto")

        cwd = Path(workdir) if workdir else Path.cwd()

        if framework == "auto":
            framework = FrameworkDetector.detect_framework(cwd)

        log: list[dict[str, Any]] = []

        for iteration in range(1, max_iterations + 1):
            # 1. 运行测试
            result = await TestRunner.run_tests(
                framework=framework,
                path=test_path,
                workdir=str(cwd),
            )

            entry: dict[str, Any] = {
                "iteration": iteration,
                "passed": result.passed,
                "failed": result.failed,
                "errors_fixed": [],
            }

            if result.failed == 0:
                entry["status"] = "all_passed"
                log.append(entry)
                break

            # 2. 对每个失败尝试修复
            for error in result.errors:
                fix_result = await self._attempt_fix(cwd, error, framework)
                entry["errors_fixed"].append(fix_result)

            entry["status"] = "attempted"
            log.append(entry)

        # 最终运行一次
        final = await TestRunner.run_tests(
            framework=framework,
            path=test_path,
            workdir=str(cwd),
        )

        summary = {
            "total_iterations": len(log),
            "final_passed": final.passed,
            "final_failed": final.failed,
            "iterations": log,
            "raw_output": final.raw_output[:2000],
        }

        return json.dumps(summary, indent=2, ensure_ascii=False)

    async def _attempt_fix(
        self, cwd: Path, error: dict[str, Any], framework: str
    ) -> dict[str, Any]:
        """尝试修复单个测试错误"""
        file_path = error.get("file", "")
        line = error.get("line", 0)
        message = error.get("message", "")

        fix_info: dict[str, Any] = {
            "file": file_path,
            "line": line,
            "message": message,
            "action": "none",
        }

        if not file_path:
            fix_info["action"] = "skipped_no_file"
            return fix_info

        full_path = cwd / file_path
        if not full_path.exists():
            fix_info["action"] = "skipped_file_not_found"
            return fix_info

        try:
            content = full_path.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)

            # 简单启发式修复策略：
            # - ImportError / ModuleNotFoundError → 尝试添加导入
            # - TypeError: missing positional arg → 不做自动修复
            # - NameError → 不做自动修复
            # - 其他 → 标记为需要人工干预

            if "ImportError" in message or "ModuleNotFoundError" in message:
                # 尝试从错误消息提取模块名
                mod_match = re.search(r"No module named '(\S+)'", message)
                if mod_match:
                    module = mod_match.group(1)
                    # 在文件头添加 import
                    insert_line = 0
                    for i, l in enumerate(lines):
                        if l.strip().startswith(("import ", "from ")):
                            insert_line = i + 1
                    lines.insert(insert_line, f"import {module}\n")
                    full_path.write_text("".join(lines), encoding="utf-8")
                    fix_info["action"] = f"added_import_{module}"
                    return fix_info

            # 标记为需要人工
            fix_info["action"] = "requires_manual_fix"

        except Exception as e:
            fix_info["action"] = f"error: {e}"

        return fix_info


# ---------------------------------------------------------------------------
# 工具 3: tdd_watch
# ---------------------------------------------------------------------------

class TddWatchTool(Tool):
    """监听文件变化并返回最新测试结果"""

    @property
    def name(self) -> str:
        return "tdd_watch"

    @property
    def description(self) -> str:
        return (
            "Check for file changes since last run and re-run tests.\n"
            "Returns changed file list + test results."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to watch",
                },
                "pattern": {
                    "type": "string",
                    "description": "File glob pattern (default: '*.py *.ts *.js')",
                    "default": "*.py *.ts *.js",
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory (optional)",
                },
                "framework": {
                    "type": "string",
                    "description": "Test framework (default: auto)",
                    "enum": ["pytest", "jest", "vitest", "go test", "auto"],
                    "default": "auto",
                },
            },
            "required": ["path"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    # 类级别的上次扫描快照 {dir_path: {file: mtime}}
    _snapshots: dict[str, dict[str, float]] = {}

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path", ".")
        pattern = kwargs.get("pattern", "*.py *.ts *.js")
        workdir = kwargs.get("workdir")
        framework = kwargs.get("framework", "auto")

        cwd = Path(workdir) if workdir else Path.cwd()
        watch_dir = cwd / path

        if not watch_dir.is_dir():
            return json.dumps({"error": f"Directory not found: {watch_dir}"})

        # 收集当前文件快照
        patterns = pattern.split()
        current: dict[str, float] = {}
        for pat in patterns:
            for f in watch_dir.rglob(pat):
                if f.is_file():
                    try:
                        current[str(f.relative_to(cwd))] = f.stat().st_mtime
                    except OSError:
                        pass

        # 对比上次快照
        snapshot_key = str(watch_dir.resolve())
        prev = TddWatchTool._snapshots.get(snapshot_key, {})

        changed = [
            f for f, mtime in current.items()
            if f not in prev or mtime > prev[f]
        ]

        # 更新快照
        TddWatchTool._snapshots[snapshot_key] = current

        # 运行测试
        if framework == "auto":
            framework = FrameworkDetector.detect_framework(cwd)

        result = await TestRunner.run_tests(
            framework=framework,
            path=path,
            workdir=str(cwd),
        )

        return json.dumps({
            "changed_files": changed,
            "test_result": result.to_dict(),
        }, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def register_tdd_tools() -> None:
    """注册所有 TDD 工具"""
    from .registry import register_tool

    register_tool(TddRunTestsTool())
    register_tool(TddFixLoopTool())
    register_tool(TddWatchTool())
