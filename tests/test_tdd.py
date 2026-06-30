"""
测试 TDD 闭环工具

覆盖：
- TestResult 数据结构
- FrameworkDetector 框架检测
- TestRunner 输出解析
- tdd_run_tests 工具
- tdd_fix_loop 工具
- tdd_watch 工具
"""
import json
import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from coding_agent.tools.tdd_ops import (
    TestResult,
    FrameworkDetector,
    TestRunner,
    TddRunTestsTool,
    TddFixLoopTool,
    TddWatchTool,
    register_tdd_tools,
)
from coding_agent.tools.base import ToolPermission


# ---------------------------------------------------------------------------
# TestResult
# ---------------------------------------------------------------------------

class TestTestResult:
    def test_defaults(self):
        r = TestResult()
        assert r.passed == 0
        assert r.failed == 0
        assert r.errors == []
        assert r.raw_output == ""

    def test_to_dict(self):
        r = TestResult(passed=3, failed=1, errors=[{"file": "a.py", "line": 5, "message": "bad"}], raw_output="raw")
        d = r.to_dict()
        assert d["passed"] == 3
        assert d["failed"] == 1
        assert len(d["errors"]) == 1
        assert d["raw_output"] == "raw"


# ---------------------------------------------------------------------------
# FrameworkDetector
# ---------------------------------------------------------------------------

class TestFrameworkDetector:
    def test_detect_language_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        assert FrameworkDetector.detect_language(tmp_path) == "python"

    def test_detect_language_javascript(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert FrameworkDetector.detect_language(tmp_path) == "javascript"

    def test_detect_language_go(self, tmp_path):
        (tmp_path / "go.mod").write_text("module test")
        assert FrameworkDetector.detect_language(tmp_path) == "go"

    def test_detect_language_unknown(self, tmp_path):
        assert FrameworkDetector.detect_language(tmp_path) is None

    def test_detect_framework_python_default(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n")
        fw = FrameworkDetector.detect_framework(tmp_path, "python")
        assert fw == "pytest"

    def test_detect_framework_js_vitest(self, tmp_path):
        pkg = {"devDependencies": {"vitest": "^1.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        fw = FrameworkDetector.detect_framework(tmp_path, "javascript")
        assert fw == "vitest"

    def test_detect_framework_js_jest(self, tmp_path):
        pkg = {"devDependencies": {"jest": "^29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        fw = FrameworkDetector.detect_framework(tmp_path, "javascript")
        assert fw == "jest"

    def test_detect_framework_js_mocha(self, tmp_path):
        pkg = {"devDependencies": {"mocha": "^10.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        fw = FrameworkDetector.detect_framework(tmp_path, "javascript")
        assert fw == "mocha"

    def test_detect_framework_go(self, tmp_path):
        assert FrameworkDetector.detect_framework(tmp_path, "go") == "go test"


# ---------------------------------------------------------------------------
# TestRunner 输出解析
# ---------------------------------------------------------------------------

class TestRunnerParsing:
    def test_parse_pytest_passed(self):
        raw = "==== 5 passed in 1.23s ===="
        r = TestRunner._parse_pytest(raw)
        assert r.passed == 5
        assert r.failed == 0

    def test_parse_pytest_mixed(self):
        raw = "==== 3 passed, 2 failed in 0.5s ===="
        r = TestRunner._parse_pytest(raw)
        assert r.passed == 3
        assert r.failed == 2

    def test_parse_pytest_no_summary(self):
        raw = "FAILED test_foo.py::test_bar - AssertionError: bad"
        r = TestRunner._parse_pytest(raw)
        assert r.failed >= 1

    def test_parse_jest_vitest(self):
        raw = "Tests: 4 passed, 1 failed, 5 total"
        r = TestRunner._parse_jest_vitest(raw)
        assert r.passed == 4
        assert r.failed == 1

    def test_parse_go_test_pass(self):
        raw = "--- PASS: TestFoo (0.01s)\n--- PASS: TestBar (0.02s)\n"
        r = TestRunner._parse_go_test(raw, 0)
        assert r.passed == 2
        assert r.failed == 0

    def test_parse_go_test_fail(self):
        raw = "--- PASS: TestFoo (0.01s)\n--- FAIL: TestBar (0.02s)\n"
        r = TestRunner._parse_go_test(raw, 1)
        assert r.passed == 1
        assert r.failed == 1

    def test_parse_unknown_framework(self):
        r = TestRunner._parse_output("unknown", "some output", 1)
        assert r.failed == 1
        assert r.raw_output == "some output"


# ---------------------------------------------------------------------------
# tdd_run_tests 工具
# ---------------------------------------------------------------------------

class TestTddRunTestsTool:
    def test_properties(self):
        tool = TddRunTestsTool()
        assert tool.name == "tdd_run_tests"
        assert tool.permission == ToolPermission.READ
        assert "passed" in tool.description

    def test_parameters_schema(self):
        tool = TddRunTestsTool()
        params = tool.parameters
        assert params["type"] == "object"
        assert "path" in params["properties"]
        assert "framework" in params["properties"]
        fw_enum = params["properties"]["framework"]["enum"]
        assert "auto" in fw_enum
        assert "pytest" in fw_enum

    @pytest.mark.asyncio
    async def test_execute_auto_framework(self, tmp_path):
        """测试 auto 框架检测 + 模拟运行"""
        (tmp_path / "pyproject.toml").write_text("")
        tool = TddRunTestsTool()

        mock_result = TestResult(passed=2, failed=0, raw_output="==== 2 passed ====")
        with patch.object(TestRunner, "run_tests", new_callable=AsyncMock, return_value=mock_result):
            output = await tool.execute(workdir=str(tmp_path))
            data = json.loads(output)
            assert data["passed"] == 2
            assert data["failed"] == 0

    @pytest.mark.asyncio
    async def test_execute_with_failures(self, tmp_path):
        tool = TddRunTestsTool()
        mock_result = TestResult(
            passed=1,
            failed=2,
            errors=[{"file": "test_a.py", "line": 10, "message": "assert 1 == 2"}],
            raw_output="FAILED",
        )
        with patch.object(TestRunner, "run_tests", new_callable=AsyncMock, return_value=mock_result):
            output = await tool.execute(workdir=str(tmp_path))
            data = json.loads(output)
            assert data["passed"] == 1
            assert data["failed"] == 2
            assert len(data["errors"]) == 1

    @pytest.mark.asyncio
    async def test_execute_explicit_framework(self, tmp_path):
        tool = TddRunTestsTool()
        mock_result = TestResult(passed=5, failed=0, raw_output="ok")
        with patch.object(TestRunner, "run_tests", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            await tool.execute(framework="jest", workdir=str(tmp_path))
            mock_run.assert_called_once()
            assert mock_run.call_args[1]["framework"] == "jest"


# ---------------------------------------------------------------------------
# tdd_fix_loop 工具
# ---------------------------------------------------------------------------

class TestTddFixLoopTool:
    def test_properties(self):
        tool = TddFixLoopTool()
        assert tool.name == "tdd_fix_loop"
        assert tool.permission == ToolPermission.EXECUTE
        assert "MODIFIES" in tool.description

    def test_parameters_schema(self):
        tool = TddFixLoopTool()
        assert "max_iterations" in tool.parameters["properties"]

    @pytest.mark.asyncio
    async def test_execute_all_pass_first_try(self, tmp_path):
        """第一次就全部通过：不进修复循环。"""
        tool = TddFixLoopTool()
        pass_result = TestResult(passed=3, failed=0, raw_output="all passed")

        with patch.object(TestRunner, "run_tests", new_callable=AsyncMock, return_value=pass_result):
            output = await tool.execute(workdir=str(tmp_path), max_iterations=3)
            data = json.loads(output)
            assert data["final_passed"] == 3
            assert data["final_failed"] == 0
            assert data["fixed"] is True
            assert data["total_iterations"] == 0

    @pytest.mark.asyncio
    async def test_execute_no_model_returns_failures(self, tmp_path):
        """无 parent_agent（无模型）：失败时把结果交回主循环，不空转。"""
        tool = TddFixLoopTool()  # parent_agent=None
        fail_result = TestResult(passed=0, failed=1, raw_output="FAILED")

        with patch.object(TestRunner, "run_tests", new_callable=AsyncMock, return_value=fail_result):
            output = await tool.execute(workdir=str(tmp_path), max_iterations=3)
            data = json.loads(output)
            assert data["fixed"] is False
            assert data["iterations"][0]["action"] == "no_model_returned_failures"

    @pytest.mark.asyncio
    async def test_looks_like_test_guard(self):
        tool = TddFixLoopTool()
        assert tool._looks_like_test("calc_test.py") is True
        assert tool._looks_like_test("test_foo.py") is True
        assert tool._looks_like_test("foo.spec.js") is True
        assert tool._looks_like_test("calc.py") is False


# ---------------------------------------------------------------------------
# tdd_watch 工具
# ---------------------------------------------------------------------------

class TestTddWatchTool:
    def test_properties(self):
        tool = TddWatchTool()
        assert tool.name == "tdd_watch"
        assert tool.permission == ToolPermission.READ
        assert "file changes" in tool.description

    def test_parameters_schema(self):
        tool = TddWatchTool()
        assert "path" in tool.parameters["properties"]
        assert "pattern" in tool.parameters["properties"]

    @pytest.mark.asyncio
    async def test_execute_first_run(self, tmp_path):
        """首次运行，所有文件都是'变化'的"""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")
        (tmp_path / "src" / "utils.py").write_text("y = 2\n")

        tool = TddWatchTool()
        # 清除快照以确保首次运行
        TddWatchTool._snapshots.clear()

        mock_result = TestResult(passed=2, failed=0, raw_output="ok")
        with patch.object(TestRunner, "run_tests", new_callable=AsyncMock, return_value=mock_result):
            output = await tool.execute(path="src", workdir=str(tmp_path))
            data = json.loads(output)
            assert len(data["changed_files"]) == 2
            assert data["test_result"]["passed"] == 2

    @pytest.mark.asyncio
    async def test_execute_no_changes(self, tmp_path):
        """连续两次运行，第二次应无变化"""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1\n")

        tool = TddWatchTool()
        TddWatchTool._snapshots.clear()

        mock_result = TestResult(passed=1, failed=0, raw_output="ok")
        with patch.object(TestRunner, "run_tests", new_callable=AsyncMock, return_value=mock_result):
            # 第一次
            await tool.execute(path="src", workdir=str(tmp_path))
            # 第二次
            output = await tool.execute(path="src", workdir=str(tmp_path))
            data = json.loads(output)
            assert data["changed_files"] == []

    @pytest.mark.asyncio
    async def test_execute_detects_changes(self, tmp_path):
        """修改文件后应检测到变化"""
        (tmp_path / "src").mkdir()
        src = tmp_path / "src" / "main.py"
        src.write_text("x = 1\n")

        tool = TddWatchTool()
        TddWatchTool._snapshots.clear()

        mock_result = TestResult(passed=1, failed=0, raw_output="ok")
        with patch.object(TestRunner, "run_tests", new_callable=AsyncMock, return_value=mock_result):
            # 首次
            await tool.execute(path="src", workdir=str(tmp_path))

            # 修改文件
            import time
            time.sleep(0.05)  # 确保 mtime 变化
            src.write_text("x = 2\n")

            # 再次运行
            output = await tool.execute(path="src", workdir=str(tmp_path))
            data = json.loads(output)
            assert "src/main.py" in data["changed_files"]

    @pytest.mark.asyncio
    async def test_execute_dir_not_found(self, tmp_path):
        """目录不存在"""
        tool = TddWatchTool()
        output = await tool.execute(path="nonexistent", workdir=str(tmp_path))
        data = json.loads(output)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_execute_multi_pattern(self, tmp_path):
        """多文件模式匹配"""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("")
        (tmp_path / "src" / "b.ts").write_text("")
        (tmp_path / "src" / "c.js").write_text("")
        (tmp_path / "src" / "d.txt").write_text("")  # 不匹配

        tool = TddWatchTool()
        TddWatchTool._snapshots.clear()

        mock_result = TestResult(passed=0, failed=0, raw_output="ok")
        with patch.object(TestRunner, "run_tests", new_callable=AsyncMock, return_value=mock_result):
            output = await tool.execute(path="src", pattern="*.py *.ts *.js", workdir=str(tmp_path))
            data = json.loads(output)
            changed = data["changed_files"]
            assert any("a.py" in f for f in changed)
            assert any("b.ts" in f for f in changed)
            assert any("c.js" in f for f in changed)
            assert not any("d.txt" in f for f in changed)


# ---------------------------------------------------------------------------
# register_tdd_tools
# ---------------------------------------------------------------------------

class TestRegisterTddTools:
    def test_register_all(self):
        from coding_agent.tools.registry import ToolRegistry
        reg = ToolRegistry()
        register_tdd_tools(reg)
        assert reg.get_tool("tdd_run_tests") is not None
        assert reg.get_tool("tdd_fix_loop") is not None
        assert reg.get_tool("tdd_watch") is not None
