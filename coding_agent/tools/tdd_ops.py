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
    """自动修复失败测试的真实闭环（会修改代码）。

    流程：跑测试 → 失败则把失败输出 + 相关源码喂给模型 → 模型返回修正后的
    完整文件 → 写盘 → 重跑，直到通过或达到 max_iterations。

    需要 parent_agent 提供模型调用（与子代理同款 _model_call_fn）。没有模型时
    退化为只跑一次测试并把失败详情返回给主循环（让主循环里的模型自己修）。
    """

    def __init__(self, parent_agent: Any = None) -> None:
        self._parent_agent = parent_agent

    @property
    def name(self) -> str:
        return "tdd_fix_loop"

    @property
    def description(self) -> str:
        return (
            "Auto-fix failing tests in a loop: run tests → send failures + source "
            "to the model → apply the model's corrected files → re-test → repeat "
            "until pass or limit reached.\n"
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
        # 可选：限定模型只允许修改这些源文件（避免它去改测试）
        source_files = kwargs.get("source_files") or []

        cwd = Path(workdir) if workdir else Path.cwd()

        if framework == "auto":
            framework = FrameworkDetector.detect_framework(cwd)

        model_fn = getattr(self._parent_agent, "_model_call_fn", None)

        log: list[dict[str, Any]] = []
        result = await TestRunner.run_tests(
            framework=framework, path=test_path, workdir=str(cwd))

        for iteration in range(1, max_iterations + 1):
            if result.failed == 0:
                break
            entry: dict[str, Any] = {
                "iteration": iteration,
                "failed": result.failed,
                "action": "none",
            }
            if model_fn is None:
                # 无模型：把失败详情交回主循环（由主循环里的模型修）
                entry["action"] = "no_model_returned_failures"
                log.append(entry)
                break

            edited = await self._model_fix(cwd, result, source_files, model_fn)
            entry["action"] = "edited" if edited else "no_edit"
            entry["files_changed"] = edited
            log.append(entry)
            if not edited:
                break  # 模型没给出可应用的修改，停止避免空转

            # 重跑
            result = await TestRunner.run_tests(
                framework=framework, path=test_path, workdir=str(cwd))

        summary = {
            "total_iterations": len(log),
            "final_passed": result.passed,
            "final_failed": result.failed,
            "fixed": result.failed == 0,
            "iterations": log,
            "raw_output": result.raw_output[:2000],
        }
        return json.dumps(summary, indent=2, ensure_ascii=False)

    # 模型返回修正文件的标记格式（便于稳健解析）
    _FILE_RE = re.compile(
        r"<<<FILE:\s*(?P<path>[^\n>]+?)\s*>>>\n(?P<body>.*?)\n<<<END>>>",
        re.DOTALL,
    )

    async def _model_fix(self, cwd: Path, result: "TestResult",
                         source_files: list[str], model_fn: Any) -> list[str]:
        """把失败 + 源码喂给模型，应用其返回的修正文件，返回改动的文件名列表。"""
        # 收集候选源文件：优先调用方指定，否则用 cwd 下的非测试源文件
        candidates = self._collect_sources(cwd, source_files)
        if not candidates:
            return []

        src_blocks = []
        for rel, text in candidates.items():
            src_blocks.append(f"<<<FILE: {rel}>>>\n{text}\n<<<END>>>")
        prompt = (
            "Some tests are failing. Fix the SOURCE code so all tests pass. "
            "Do NOT modify the tests. Return ONLY the full corrected contents of "
            "each file you change, each wrapped exactly as:\n"
            "<<<FILE: relative/path.py>>>\n<full file content>\n<<<END>>>\n"
            "Return nothing else. If a file needs no change, omit it.\n\n"
            f"## Test failures\n```\n{result.raw_output[:4000]}\n```\n\n"
            f"## Current source files\n" + "\n\n".join(src_blocks)
        )
        try:
            resp = await model_fn([{"role": "user", "content": prompt}], [])
        except Exception:  # noqa: BLE001
            return []
        text = (resp or {}).get("content", "") if isinstance(resp, dict) else str(resp)
        changed: list[str] = []
        for m in self._FILE_RE.finditer(text or ""):
            rel = m.group("path").strip()
            body = m.group("body")
            # 安全：只允许写 cwd 内、且不是测试文件
            target = (cwd / rel).resolve()
            try:
                target.relative_to(cwd.resolve())
            except ValueError:
                continue
            if self._looks_like_test(rel):
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body, encoding="utf-8")
                changed.append(rel)
            except OSError:
                continue
        return changed

    @staticmethod
    def _looks_like_test(rel: str) -> bool:
        base = Path(rel).name.lower()
        return ("test" in base or base.endswith("_test.py")
                or base.endswith("_test.go") or "spec" in base)

    def _collect_sources(self, cwd: Path, source_files: list[str]) -> dict[str, str]:
        """返回 {相对路径: 内容}。指定了 source_files 就用它，否则扫 cwd 非测试源。"""
        out: dict[str, str] = {}
        if source_files:
            for rel in source_files:
                p = cwd / rel
                if p.is_file():
                    try:
                        out[rel] = p.read_text(encoding="utf-8")
                    except (OSError, UnicodeError):
                        pass
            return out
        exts = (".py", ".go", ".js", ".ts", ".rs", ".java", ".cpp", ".c")
        for p in sorted(cwd.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            rel = str(p.relative_to(cwd))
            if self._looks_like_test(rel) or "/." in f"/{rel}":
                continue
            try:
                out[rel] = p.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if len(out) >= 10:  # 防止上下文爆炸
                break
        return out


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

def register_tdd_tools(registry: Any = None, parent_agent: Any = None) -> None:
    """注册所有 TDD 工具。parent_agent 供 tdd_fix_loop 调用模型做真实修复。"""
    from .registry import get_registry

    reg = registry or get_registry()
    reg.register(TddRunTestsTool())
    reg.register(TddFixLoopTool(parent_agent=parent_agent))
    reg.register(TddWatchTool())
