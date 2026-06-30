"""
Slash 命令系统 - 内置命令 + 用户自定义命令

参考 Claude Code（/init、/compact 等）与 opencode（模板化自定义命令）的设计。

两类命令：
  - 内置命令：/help /tools /cost /compact /plan /clear /sessions —— 直接执行，
    返回要打印的文本，或一个动作信号。
  - 自定义命令：从 .coding-agent/commands/<name>.md 加载，文件内容是 prompt
    模板，支持 $ARGUMENTS 占位符，调用时作为用户消息注入模型。

命令解析：以 "/" 开头的输入。`/name args...`。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


# 命令执行结果：
#   ("print", text)   -> 打印 text，不进模型
#   ("prompt", text)  -> 把 text 作为用户消息送给模型
#   ("action", name)  -> 触发一个内置动作（new/quit/compact 等），由 CLI 处理
@dataclass
class CommandResult:
    kind: str   # "print" | "prompt" | "action"
    payload: str


# 内置命令处理器签名：(args, ctx) -> CommandResult
BuiltinHandler = Callable[[str, "CommandContext"], CommandResult]


@dataclass
class CommandContext:
    """传给命令处理器的运行时上下文（避免命令模块依赖 CLI 内部）。"""
    tool_names: list[str]
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_reasoning_tokens: int = 0
    cache_hit_rate: float = 0.0
    session_id: str | None = None
    turn_count: int = 0


def _cmd_help(args: str, ctx: CommandContext) -> CommandResult:
    lines = [
        "Slash commands:",
        "  /help        — show this help",
        "  /tools       — list available tools",
        "  /cost        — token usage this session",
        "  /compact     — summarize & compact the conversation now",
        "  /plan        — show the current plan",
        "  /init        — generate AGENTS.md from a repo scan",
        "  /clear, /new — start a fresh session",
        "  /sessions    — list recent sessions",
        "  /quit        — exit",
        "Custom commands live in .coding-agent/commands/<name>.md",
    ]
    return CommandResult("print", "\n".join(lines))


def _cmd_tools(args: str, ctx: CommandContext) -> CommandResult:
    names = ", ".join(sorted(ctx.tool_names))
    return CommandResult("print", f"{len(ctx.tool_names)} tools:\n  {names}")


def _cmd_cost(args: str, ctx: CommandContext) -> CommandResult:
    r = (f", reasoning {ctx.total_reasoning_tokens}"
         if ctx.total_reasoning_tokens else "")
    return CommandResult(
        "print",
        f"Tokens: {ctx.total_prompt_tokens} in / {ctx.total_completion_tokens} out{r} "
        f"(cache hits {ctx.cache_hit_rate*100:.0f}%)",
    )


def _cmd_compact(args: str, ctx: CommandContext) -> CommandResult:
    return CommandResult("action", "compact")


def _cmd_plan(args: str, ctx: CommandContext) -> CommandResult:
    return CommandResult("action", "plan")


def _cmd_clear(args: str, ctx: CommandContext) -> CommandResult:
    return CommandResult("action", "new")


def _cmd_new(args: str, ctx: CommandContext) -> CommandResult:
    return CommandResult("action", "new")


def _cmd_sessions(args: str, ctx: CommandContext) -> CommandResult:
    return CommandResult("action", "sessions")


def _cmd_quit(args: str, ctx: CommandContext) -> CommandResult:
    return CommandResult("action", "quit")


def scan_repo(root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """
    扫描仓库，收集生成 AGENTS.md 所需的事实：语言、依赖/构建文件、测试命令、
    顶层结构。纯本地、无模型调用。
    """
    base = Path(root) if root else Path.cwd()
    facts: dict[str, Any] = {"root": str(base)}

    markers = {
        "python": ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"],
        "javascript/typescript": ["package.json", "tsconfig.json"],
        "go": ["go.mod"],
        "rust": ["Cargo.toml"],
        "java": ["pom.xml", "build.gradle"],
        "ruby": ["Gemfile"],
    }
    langs = []
    present_markers = []
    for lang, files in markers.items():
        for f in files:
            if (base / f).is_file():
                langs.append(lang)
                present_markers.append(f)
                break
    facts["languages"] = langs
    facts["marker_files"] = present_markers

    # 测试命令猜测
    test_cmds = []
    if (base / "pyproject.toml").is_file() or list(base.glob("test_*.py")) or (base / "tests").is_dir():
        test_cmds.append("pytest")
    if (base / "package.json").is_file():
        test_cmds.append("npm test")
    if (base / "go.mod").is_file():
        test_cmds.append("go test ./...")
    if (base / "Cargo.toml").is_file():
        test_cmds.append("cargo test")
    facts["test_commands"] = test_cmds

    # 顶层目录与关键文件
    try:
        entries = sorted(p.name + ("/" if p.is_dir() else "")
                         for p in base.iterdir()
                         if not p.name.startswith("."))[:40]
    except OSError:
        entries = []
    facts["top_level"] = entries
    facts["has_makefile"] = (base / "Makefile").is_file()
    facts["has_readme"] = any((base / n).is_file() for n in ("README.md", "README.rst", "README"))
    return facts


def _render_init_prompt(facts: dict[str, Any]) -> str:
    """把扫描事实拼成给模型的 prompt，让它写 AGENTS.md。"""
    import json
    return (
        "Create an AGENTS.md file at the repository root that documents this project "
        "for future AI coding agents. Base it on these scanned facts, and read a few "
        "key files (README, build config, entry points) to fill in specifics:\n\n"
        f"{json.dumps(facts, ensure_ascii=False, indent=2)}\n\n"
        "AGENTS.md should cover: what the project is, how to install/build, how to run "
        "tests (use the detected commands), the directory layout, and any conventions "
        "you can infer. Keep it concise and accurate — don't invent things you can't "
        "verify. Write the file with file_write."
    )


def _cmd_init(args: str, ctx: CommandContext) -> CommandResult:
    facts = scan_repo()
    return CommandResult("prompt", _render_init_prompt(facts))


def _cmd_plan_mode(args: str, ctx: CommandContext) -> CommandResult:
    return CommandResult("action", "plan_mode")


def _cmd_agents(args: str, ctx: CommandContext) -> CommandResult:
    """列出可用的命名 agent profiles。"""
    from .agent_profiles import discover_agents, render_available_agents
    return CommandResult("print", render_available_agents(discover_agents()))


def _cmd_agent(args: str, ctx: CommandContext) -> CommandResult:
    """切换当前会话的活动 agent profile：/agent <name>（无参=显示用法）。"""
    name = args.strip()
    if not name:
        return CommandResult("print", "Usage: /agent <name>  (see /agents for the list)")
    # 交给 CLI 处理实际切换（需要访问运行时 state / agent_loop）
    return CommandResult("action", f"agent:{name}")


BUILTINS: dict[str, BuiltinHandler] = {
    "help": _cmd_help,
    "tools": _cmd_tools,
    "cost": _cmd_cost,
    "compact": _cmd_compact,
    "plan": _cmd_plan,
    "plan-mode": _cmd_plan_mode,
    "agents": _cmd_agents,
    "agent": _cmd_agent,
    "clear": _cmd_clear,
    "new": _cmd_new,
    "sessions": _cmd_sessions,
    "init": _cmd_init,
    "quit": _cmd_quit,
    "exit": _cmd_quit,
}


def is_command(text: str) -> bool:
    return text.startswith("/") and len(text) > 1


def load_custom_commands(root: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """加载自定义命令（名→模板文本）。

    两类来源合并（同名时 .md 命令文件优先于 skill）：
      1. slash:true 的 skills（其正文作为 prompt 模板）
      2. .coding-agent/commands/*.md 命令文件
    """
    out: dict[str, str] = {}
    # 1. slash:true skills
    try:
        from .skills import discover_skills
        for name, info in discover_skills(cwd=root).items():
            if info.slash:
                out[name] = info.content
    except Exception:
        pass
    # 2. .coding-agent/commands/*.md（覆盖同名 skill）
    base = Path(root) if root else Path.cwd()
    cmd_dir = base / ".coding-agent" / "commands"
    if cmd_dir.is_dir():
        for p in cmd_dir.glob("*.md"):
            try:
                out[p.stem] = p.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
    return out


def dispatch(text: str, ctx: CommandContext,
             custom: dict[str, str] | None = None) -> CommandResult:
    """
    解析并执行一条 slash 命令。

    优先级：内置命令 > 自定义命令。未知命令返回提示。
    """
    body = text[1:].strip()
    parts = body.split(None, 1)
    name = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if name in BUILTINS:
        return BUILTINS[name](args, ctx)

    custom = custom if custom is not None else load_custom_commands()
    if name in custom:
        template = custom[name]
        # $ARGUMENTS 占位符替换；没有占位符则把 args 追加到末尾
        if "$ARGUMENTS" in template:
            prompt = template.replace("$ARGUMENTS", args)
        else:
            prompt = template + (f"\n\n{args}" if args else "")
        return CommandResult("prompt", prompt)

    return CommandResult("print", f"Unknown command: /{name}. Try /help.")
