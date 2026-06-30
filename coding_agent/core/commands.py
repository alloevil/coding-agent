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


BUILTINS: dict[str, BuiltinHandler] = {
    "help": _cmd_help,
    "tools": _cmd_tools,
    "cost": _cmd_cost,
    "compact": _cmd_compact,
    "plan": _cmd_plan,
    "clear": _cmd_clear,
    "new": _cmd_new,
    "sessions": _cmd_sessions,
    "quit": _cmd_quit,
    "exit": _cmd_quit,
}


def is_command(text: str) -> bool:
    return text.startswith("/") and len(text) > 1


def load_custom_commands(root: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """加载 .coding-agent/commands/*.md 自定义命令（名→模板文本）。"""
    base = Path(root) if root else Path.cwd()
    cmd_dir = base / ".coding-agent" / "commands"
    out: dict[str, str] = {}
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
