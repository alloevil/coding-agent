"""
Skills 发现与加载 - 渐进式披露（progressive disclosure）

参考 Claude Code 与 opencode 的 skill 机制（opencode 为开源参考）：每个 skill 是
一个目录，内含一个 SKILL.md，其 YAML frontmatter 提供 name / description。

核心思想（渐进式披露）：
  - 每轮只把所有 skill 的 name+description（很短）注入上下文，开销极小；
  - 完整的（可能数 KB 的）指令正文按需通过 `skill` 工具加载，仅当模型判断相关时。

发现来源（后者覆盖前者的同名 skill）：
  1. ~/.claude/skills/<name>/SKILL.md           —— 与 Claude Code 互通
  2. ~/.config/coding-agent/skills/<name>/SKILL.md  —— 全局
  3. ./.coding-agent/skills/<name>/SKILL.md      —— 项目级（优先级最高）

只读、纯本地、无模型调用。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SkillInfo:
    """一个已发现的 skill。"""
    name: str
    description: str
    location: str   # SKILL.md 的绝对路径
    content: str    # frontmatter 之后的正文（不含 frontmatter）
    slash: bool = False   # frontmatter slash: true → 可作为 /name 斜杠命令调用


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """
    解析极简 YAML frontmatter：以 '---' 开头、再以 '---' 结束的块。

    只支持 `key: value` 的扁平键值（skill 只用到 name/description/slash），
    避免引入 YAML 依赖。返回 (frontmatter_dict, body)。无 frontmatter 时
    返回 ({}, 原文)。
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    # 第一行是 '---'；找下一处单独的 '---'
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    data: dict[str, str] = {}
    for raw in lines[1:end]:
        if not raw.strip() or ":" not in raw:
            continue
        key, _, val = raw.partition(":")
        data[key.strip()] = val.strip().strip('"').strip("'")
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return data, body


def _safe_name(name: str) -> bool:
    """拒绝路径穿越 / 分隔符 —— skill 名必须是单层标识符。"""
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or os.sep in name:
        return False
    if name.startswith("."):
        return False
    return True


def skill_dirs(cwd: str | os.PathLike[str] | None = None,
               home: str | os.PathLike[str] | None = None) -> list[Path]:
    """返回按优先级从低到高排列的 skill 根目录列表（后者覆盖前者）。"""
    base = Path(cwd) if cwd else Path.cwd()
    cfg_home = Path(home) if home else Path.home()
    return [
        cfg_home / ".claude" / "skills",
        cfg_home / ".config" / "coding-agent" / "skills",
        base / ".coding-agent" / "skills",
    ]


def discover_skills(cwd: str | os.PathLike[str] | None = None,
                    home: str | os.PathLike[str] | None = None) -> dict[str, SkillInfo]:
    """
    扫描所有来源，返回 {name: SkillInfo}。同名时高优先级目录覆盖低优先级。

    name 取 frontmatter.name，缺省时回退为 skill 目录名。
    """
    found: dict[str, SkillInfo] = {}
    for root in skill_dirs(cwd, home):
        if not root.is_dir():
            continue
        try:
            entries = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for d in entries:
            skill_md = d / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            fm, body = _parse_frontmatter(text)
            name = fm.get("name") or d.name
            if not _safe_name(name):
                continue
            found[name] = SkillInfo(
                name=name,
                description=fm.get("description", ""),
                location=str(skill_md.resolve()),
                content=body,
                slash=str(fm.get("slash", "")).strip().lower() in ("true", "yes", "1"),
            )
    return found


def render_available_skills(skills: dict[str, SkillInfo]) -> str:
    """
    把可用 skills 渲染成注入上下文的简短清单（只含 name + description）。

    没有 skill 时返回空串（调用方据此跳过注入）。
    """
    if not skills:
        return ""
    lines = ["<available_skills>",
             "These skills are available. When a task matches one, call the `skill` "
             "tool with its name to load its full instructions before proceeding."]
    for s in sorted(skills.values(), key=lambda x: x.name):
        desc = s.description or "(no description)"
        lines.append(f"- {s.name}: {desc}")
    lines.append("</available_skills>")
    return "\n".join(lines)


def load_skill(name: str, cwd: str | os.PathLike[str] | None = None,
               home: str | os.PathLike[str] | None = None) -> SkillInfo | None:
    """按名加载单个 skill；不存在或名称不安全则返回 None。"""
    if not _safe_name(name):
        return None
    return discover_skills(cwd, home).get(name)


def skill_bundled_files(info: SkillInfo, limit: int = 20) -> list[str]:
    """列出 skill 目录内除 SKILL.md 外的文件（绝对路径），供模型按需读取。"""
    skill_dir = Path(info.location).parent
    out: list[str] = []
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file() and p.name != "SKILL.md":
            out.append(str(p.resolve()))
            if len(out) >= limit:
                break
    return out


def render_skill_content(info: SkillInfo, files: list[str] | None = None) -> str:
    """把一个 skill 渲染成工具返回正文：正文 + 基目录 + 文件清单。"""
    skill_dir = str(Path(info.location).parent)
    parts = [
        f'<skill_content name="{info.name}">',
        f"# Skill: {info.name}",
        "",
        info.content.strip(),
        "",
        f"Base directory for this skill: {skill_dir}",
        "Relative paths in this skill (scripts/, reference/, ...) are relative to "
        "this base directory.",
    ]
    if files:
        parts.append("")
        parts.append("<skill_files>")
        parts.extend(files)
        parts.append("</skill_files>")
    parts.append("</skill_content>")
    return "\n".join(parts)
