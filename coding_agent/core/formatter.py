"""
写后自动格式化 - 编辑/写入文件后用合适的格式化器规范化

参考 opencode 的 format/formatter.ts（开源参考）：file_write / file_edit /
apply_patch 成功后，按文件扩展名选格式化器，若本机装了就就地格式化。

设计为非阻塞、尽力而为：
  - 格式化器没装 → 静默跳过（不报错、不阻断写入）
  - 格式化失败（语法错误等）→ 跳过，写入照常成功
  - 多个候选按顺序取第一个可用的（如 .py 优先 ruff，回退 black）

格式化在文件已经落盘之后原地进行（多数格式化器 -w/-i/--write 就地改写）。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


# 每个扩展名对应一串候选格式化器；每个候选是 (探测用的可执行名, 命令模板)。
# 命令模板里的 "$FILE" 会被替换为目标文件路径。按顺序取第一个 which 命中的。
_FORMATTERS: dict[str, list[tuple[str, list[str]]]] = {
    ".py": [("ruff", ["ruff", "format", "$FILE"]),
            ("black", ["black", "-q", "$FILE"])],
    ".pyi": [("ruff", ["ruff", "format", "$FILE"]),
             ("black", ["black", "-q", "$FILE"])],
    ".go": [("gofmt", ["gofmt", "-w", "$FILE"])],
    ".rs": [("rustfmt", ["rustfmt", "$FILE"])],
    ".js": [("prettier", ["prettier", "--write", "$FILE"])],
    ".jsx": [("prettier", ["prettier", "--write", "$FILE"])],
    ".ts": [("prettier", ["prettier", "--write", "$FILE"])],
    ".tsx": [("prettier", ["prettier", "--write", "$FILE"])],
    ".json": [("prettier", ["prettier", "--write", "$FILE"])],
    ".css": [("prettier", ["prettier", "--write", "$FILE"])],
    ".md": [("prettier", ["prettier", "--write", "$FILE"])],
    ".c": [("clang-format", ["clang-format", "-i", "$FILE"])],
    ".cc": [("clang-format", ["clang-format", "-i", "$FILE"])],
    ".cpp": [("clang-format", ["clang-format", "-i", "$FILE"])],
    ".h": [("clang-format", ["clang-format", "-i", "$FILE"])],
    ".hpp": [("clang-format", ["clang-format", "-i", "$FILE"])],
}

# 全局开关（CLI 可通过 config 关闭）。
_ENABLED = True


def set_enabled(enabled: bool) -> None:
    global _ENABLED
    _ENABLED = bool(enabled)


def _pick_command(path: str) -> list[str] | None:
    """为某文件挑一个可用的格式化命令（已展开 $FILE）；无则 None。"""
    ext = Path(path).suffix.lower()
    candidates = _FORMATTERS.get(ext)
    if not candidates:
        return None
    for exe, template in candidates:
        if shutil.which(exe):
            return [path if tok == "$FILE" else tok for tok in template]
    return None


def format_file(path: str, timeout: float = 10.0) -> str:
    """
    尽力格式化一个文件，返回一段可附加到工具结果的提示（无操作则空串）。

    非阻塞：格式化器缺失/失败/超时都安静跳过，绝不影响写入结果。
    """
    if not _ENABLED:
        return ""
    try:
        if not Path(path).is_file():
            return ""
    except OSError:
        return ""
    cmd = _pick_command(path)
    if not cmd:
        return ""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode == 0:
        return f"\n🎨 Formatted with {cmd[0]}."
    return ""  # 格式化失败（语法错误等）→ 安静跳过
