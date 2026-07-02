"""
测试 slash 命令系统
"""
from coding_agent.core.commands import (
    is_command, dispatch, load_custom_commands, CommandContext, CommandResult,
)


def _ctx(**kw):
    base = dict(tool_names=["file_read", "grep", "shell_exec"])
    base.update(kw)
    return CommandContext(**base)


def test_is_command():
    assert is_command("/help")
    assert not is_command("hello")
    assert not is_command("/")  # 单独斜杠不算


def test_help_lists_commands():
    r = dispatch("/help", _ctx())
    assert r.kind == "print"
    assert "/compact" in r.payload
    assert "/resume" in r.payload   # newly documented


def test_resume_is_sessions_alias():
    assert dispatch("/resume", _ctx()).payload == "sessions"
    assert dispatch("/sessions", _ctx()).payload == "sessions"


def test_diff_and_context_commands():
    assert dispatch("/diff", _ctx()).payload == "diff"
    r = dispatch("/context", _ctx())
    assert r.kind == "print" and "tokens" in r.payload


def test_recap_is_prompt():
    r = dispatch("/recap", _ctx())
    assert r.kind == "prompt" and "recap" in r.payload.lower()


def test_review_is_prompt_and_takes_focus():
    r = dispatch("/review", _ctx())
    assert r.kind == "prompt" and "review" in r.payload.lower()
    r2 = dispatch("/review performance", _ctx())
    assert "performance" in r2.payload


def test_memory_and_export_actions():
    assert dispatch("/memory", _ctx()).payload == "memory:"
    assert dispatch("/memory add foo bar", _ctx()).payload == "memory:add foo bar"
    assert dispatch("/export", _ctx()).payload == "export:"
    assert dispatch("/export out.md", _ctx()).payload == "export:out.md"


def test_tools_lists_registered():
    r = dispatch("/tools", _ctx())
    assert r.kind == "print"
    assert "file_read" in r.payload and "3 tools" in r.payload


def test_cost_reports_usage():
    r = dispatch("/cost", _ctx(total_prompt_tokens=100, total_completion_tokens=20,
                               total_reasoning_tokens=8, cache_hit_rate=0.5))
    assert "100 in" in r.payload and "reasoning 8" in r.payload and "50%" in r.payload


def test_action_commands():
    assert dispatch("/quit", _ctx()).payload == "quit"
    assert dispatch("/new", _ctx()).payload == "new"
    assert dispatch("/clear", _ctx()).payload == "new"
    assert dispatch("/compact", _ctx()).kind == "action"


def test_unknown_command():
    r = dispatch("/frobnicate", _ctx(), custom={})
    assert r.kind == "print"
    assert "Unknown command" in r.payload


def test_custom_command_with_arguments():
    custom = {"review": "Review this code for bugs:\n$ARGUMENTS"}
    r = dispatch("/review the auth module", _ctx(), custom=custom)
    assert r.kind == "prompt"
    assert r.payload == "Review this code for bugs:\nthe auth module"


def test_custom_command_appends_args_without_placeholder():
    custom = {"explain": "Explain the following."}
    r = dispatch("/explain foo.py", _ctx(), custom=custom)
    assert r.kind == "prompt"
    assert r.payload == "Explain the following.\n\nfoo.py"


def test_load_custom_commands(tmp_path):
    d = tmp_path / ".coding-agent" / "commands"
    d.mkdir(parents=True)
    (d / "review.md").write_text("Review: $ARGUMENTS")
    (d / "test.md").write_text("Write tests")
    cmds = load_custom_commands(tmp_path)
    assert set(cmds) == {"review", "test"}
    assert cmds["review"] == "Review: $ARGUMENTS"


def test_builtin_beats_custom():
    # 受保护的核心命令（help）不可被自定义覆盖，保证可发现性
    r = dispatch("/help", _ctx(), custom={"help": "custom help"})
    assert "/compact" in r.payload


def test_custom_overrides_non_protected_builtin():
    # 非受保护的内置命令（review）允许被同名自定义命令覆盖
    r = dispatch("/review the auth module", _ctx(),
                 custom={"review": "MY REVIEW:\n$ARGUMENTS"})
    assert r.payload == "MY REVIEW:\nthe auth module"


# ── /init ─────────────────────────────────────────────────────────────────
from coding_agent.core.commands import scan_repo


def test_scan_repo_detects_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    facts = scan_repo(tmp_path)
    assert "python" in facts["languages"]
    assert "pytest" in facts["test_commands"]
    assert "src/" in facts["top_level"]


def test_scan_repo_detects_node(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    facts = scan_repo(tmp_path)
    assert "javascript/typescript" in facts["languages"]
    assert "npm test" in facts["test_commands"]


def test_init_command_returns_prompt(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module x\n")
    monkeypatch.chdir(tmp_path)
    r = dispatch("/init", _ctx())
    assert r.kind == "prompt"
    assert "AGENTS.md" in r.payload
    assert "go test ./..." in r.payload  # detected test command embedded
