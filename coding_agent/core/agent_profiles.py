"""
命名 Agent Profile - 可复用的 agent 配置（build / plan / 自定义角色）

参考 opencode 的 agent/mode 定义（开源参考）：从 markdown 文件加载命名 agent，
每个有自己的 system prompt（正文）+ frontmatter 配置：

  ---
  name: reviewer                 # 缺省用文件名
  description: Read-only code reviewer
  model: gpt-5-mini             # 可选，覆盖默认模型
  mode: subagent                # primary | subagent（缺省 primary）
  temperature: 0.2              # 可选
  tools: file_read, grep, file_search   # 可选：白名单（只允许这些）
  deny_tools: file_write, shell_exec    # 可选：黑名单（禁用这些）
  ---
  You are a meticulous code reviewer. ...

发现来源（后者覆盖前者同名）：
  1. ~/.config/coding-agent/agents/*.md   —— 全局
  2. ./.coding-agent/agents/*.md           —— 项目级（优先级高）

只读、纯本地、无模型调用。frontmatter 解析复用 skills 的极简解析器
（不引入 YAML 依赖）；tools/deny_tools 用逗号分隔列表表达，避免嵌套。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .skills import _parse_frontmatter, _safe_name


# 合法的 mode 取值
_VALID_MODES = ("primary", "subagent")


@dataclass
class AgentProfile:
    """一个命名 agent 配置。"""
    name: str
    description: str = ""
    model: str | None = None
    mode: str = "primary"
    temperature: float | None = None
    system_prompt: str = ""
    location: str = ""
    # 工具过滤：allow 非空 → 只允许这些；deny → 在其余基础上再禁用这些。
    allow_tools: list[str] = field(default_factory=list)
    deny_tools: list[str] = field(default_factory=list)

    def tool_allowed(self, tool_name: str) -> bool:
        """该 profile 是否允许调用某工具。"""
        if self.allow_tools and tool_name not in self.allow_tools:
            return False
        if tool_name in self.deny_tools:
            return False
        return True


def _parse_list(val: str) -> list[str]:
    """把 'a, b ,c' 解析成 ['a','b','c']（去空白、去空项）。"""
    return [item.strip() for item in val.split(",") if item.strip()]


def _parse_temperature(val: str) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def agent_dirs(cwd: str | os.PathLike[str] | None = None,
               home: str | os.PathLike[str] | None = None) -> list[Path]:
    """返回按优先级从低到高排列的 agent 目录（后者覆盖前者）。"""
    base = Path(cwd) if cwd else Path.cwd()
    cfg_home = Path(home) if home else Path.home()
    return [
        cfg_home / ".config" / "coding-agent" / "agents",
        base / ".coding-agent" / "agents",
    ]


def discover_agents(cwd: str | os.PathLike[str] | None = None,
                    home: str | os.PathLike[str] | None = None) -> dict[str, AgentProfile]:
    """扫描所有来源，返回 {name: AgentProfile}。同名时高优先级目录覆盖。"""
    found: dict[str, AgentProfile] = {}
    for root in agent_dirs(cwd, home):
        if not root.is_dir():
            continue
        try:
            files = sorted(root.glob("*.md"))
        except OSError:
            continue
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            fm, body = _parse_frontmatter(text)
            name = fm.get("name") or f.stem
            if not _safe_name(name):
                continue
            mode = (fm.get("mode") or "primary").strip().lower()
            if mode not in _VALID_MODES:
                mode = "primary"
            found[name] = AgentProfile(
                name=name,
                description=fm.get("description", ""),
                model=(fm.get("model") or None),
                mode=mode,
                temperature=_parse_temperature(fm.get("temperature", "")),
                system_prompt=body.strip(),
                location=str(f.resolve()),
                allow_tools=_parse_list(fm.get("tools", "")),
                deny_tools=_parse_list(fm.get("deny_tools", "")),
            )
    return found


def load_agent(name: str, cwd: str | os.PathLike[str] | None = None,
               home: str | os.PathLike[str] | None = None) -> AgentProfile | None:
    """按名加载一个 agent profile；不存在或名称不安全则返回 None。"""
    if not _safe_name(name):
        return None
    return discover_agents(cwd, home).get(name)


def render_available_agents(agents: dict[str, AgentProfile]) -> str:
    """渲染 agent 列表（供 /agents 命令展示）。空则返回提示串。"""
    if not agents:
        return "No custom agents defined. Add one at .coding-agent/agents/<name>.md"
    lines = ["Available agents:"]
    for a in sorted(agents.values(), key=lambda x: x.name):
        tags = []
        if a.mode != "primary":
            tags.append(a.mode)
        if a.model:
            tags.append(a.model)
        suffix = f" [{', '.join(tags)}]" if tags else ""
        desc = a.description or "(no description)"
        lines.append(f"  {a.name}{suffix} — {desc}")
    return "\n".join(lines)
