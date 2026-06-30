"""
项目上下文加载 - AGENTS.md / CLAUDE.md 层级合并

参考 Codex 的 AGENTS.md 与 Claude Code 的 CLAUDE.md 设计：
- 从当前工作目录向上walk到仓库根（.git）或文件系统根
- 收集每一层的 AGENTS.md / CLAUDE.md
- 另外加载一个全局文件（~/.config/coding-agent/AGENTS.md）
- 合并顺序：全局 -> 仓库根 -> ... -> cwd（最具体的 cwd 指令排在最后，优先级最高）

合并后的文本作为一段独立的 system 上下文注入，让模型在每个会话都知道
项目的约定（构建命令、风格、禁止改动的区域等），而无需每轮重新发现。
"""
from __future__ import annotations

import os
from pathlib import Path

# 支持的上下文文件名（两种生态都识别）
CONTEXT_FILENAMES = ("AGENTS.md", "CLAUDE.md")

# 单个文件读取上限，避免超大文件撑爆 context
MAX_FILE_BYTES = 32 * 1024

# 合并后的总上限
MAX_TOTAL_BYTES = 64 * 1024


def _global_context_path() -> Path:
    """全局上下文文件位置（可被 CODING_AGENT_HOME 覆盖）。"""
    home = os.environ.get("CODING_AGENT_HOME")
    base = Path(home) if home else Path.home() / ".config" / "coding-agent"
    return base / "AGENTS.md"


def _find_repo_root(start: Path) -> Path | None:
    """从 start 向上查找包含 .git 的目录；找不到返回 None。"""
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _read_capped(path: Path) -> str | None:
    """读取文件，超过上限则截断。读取失败返回 None。"""
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None
    if not data.strip():
        return None
    if len(data) > MAX_FILE_BYTES:
        data = data[:MAX_FILE_BYTES] + "\n... (truncated)"
    return data


def discover_context_files(cwd: str | os.PathLike[str] | None = None) -> list[Path]:
    """
    发现要加载的上下文文件，按合并顺序返回（全局优先，cwd 最后）。

    去重：同一目录下若 AGENTS.md 与 CLAUDE.md 同时存在，两者都加载
    （AGENTS.md 在前）。同一路径不重复。
    """
    start = Path(cwd) if cwd else Path.cwd()
    start = start.resolve()

    # 1. 确定从仓库根到 cwd 的目录链（外 -> 内）
    repo_root = _find_repo_root(start)
    if repo_root is not None:
        chain: list[Path] = []
        cur = start
        while True:
            chain.append(cur)
            if cur == repo_root or cur == cur.parent:
                break
            cur = cur.parent
        chain.reverse()  # 仓库根在前，cwd 在后
    else:
        # 不在 git 仓库中：只看 cwd
        chain = [start]

    ordered: list[Path] = []
    seen: set[Path] = set()

    # 2. 全局文件最先
    gpath = _global_context_path()
    if gpath.is_file():
        ordered.append(gpath)
        seen.add(gpath)

    # 3. 目录链中每一层的上下文文件
    for d in chain:
        for fname in CONTEXT_FILENAMES:
            p = d / fname
            rp = p.resolve()
            if p.is_file() and rp not in seen:
                ordered.append(p)
                seen.add(rp)

    return ordered


def load_project_context(cwd: str | os.PathLike[str] | None = None) -> str:
    """
    加载并合并项目上下文，返回一段 markdown 文本（可能为空字符串）。

    每个文件以其来源路径作为小标题，便于模型理解指令的作用域。
    """
    files = discover_context_files(cwd)
    if not files:
        return ""

    parts: list[str] = []
    total = 0
    for path in files:
        content = _read_capped(path)
        if content is None:
            continue
        try:
            label = str(path.relative_to(Path.cwd()))
        except ValueError:
            label = str(path)
        block = f"### Project instructions from `{label}`\n\n{content}"
        if total + len(block) > MAX_TOTAL_BYTES:
            break
        parts.append(block)
        total += len(block)

    if not parts:
        return ""

    header = (
        "The following are project-specific instructions and conventions. "
        "Treat them as authoritative for this codebase; more specific "
        "(deeper-nested) instructions take precedence over broader ones."
    )
    return header + "\n\n" + "\n\n---\n\n".join(parts)
