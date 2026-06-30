"""
apply_patch - 多文件原子补丁工具

参考 Codex 的 apply_patch：一次工具调用即可对多个文件做
新增 / 修改 / 删除，先全部校验再统一落盘，任一 hunk 失败则整体回滚。
相比单次唯一字符串替换的 file_edit，更适合真实的多处 / 多文件改动。

补丁格式（受 Codex V4A 启发的简化版）：

    *** Begin Patch
    *** Add File: path/to/new.py
    +line 1
    +line 2
    *** Update File: path/to/existing.py
    @@
    -old line
    +new line
    *** Delete File: path/to/gone.py
    *** End Patch

Update 语义：每个 hunk 用 `-` 行表示要删除的原文、`+` 行表示新增；
无前缀的行作为上下文用于定位。引擎在文件中查找上下文+删除行构成的块，
用上下文+新增行替换之。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Tool, ToolPermission

BEGIN = "*** Begin Patch"
END = "*** End Patch"
ADD = "*** Add File: "
UPDATE = "*** Update File: "
DELETE = "*** Delete File: "
HUNK_SEP = "@@"


class PatchError(Exception):
    """补丁解析或应用错误。"""


# ── 解析 ────────────────────────────────────────────────────────────────

def parse_patch(text: str) -> list[dict[str, Any]]:
    """
    把补丁文本解析为操作列表。每个操作：
      {"op": "add",    "path": str, "content": str}
      {"op": "delete", "path": str}
      {"op": "update", "path": str, "hunks": [{"context_del": [str], "add": [str]}]}
    """
    lines = text.splitlines()
    # 容忍前后空白行
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if not lines or lines[0].strip() != BEGIN:
        raise PatchError(f"Patch must start with '{BEGIN}'")
    if lines[-1].strip() != END:
        raise PatchError(f"Patch must end with '{END}'")

    ops: list[dict[str, Any]] = []
    i = 1
    body = lines[1:-1]

    while i - 1 < len(body):
        line = body[i - 1]
        if line.startswith(ADD):
            path = line[len(ADD):].strip()
            i += 1
            content_lines: list[str] = []
            while i - 1 < len(body) and not body[i - 1].startswith("*** "):
                cur = body[i - 1]
                if not cur.startswith("+"):
                    raise PatchError(
                        f"Add File hunk lines must start with '+': {cur!r}"
                    )
                content_lines.append(cur[1:])
                i += 1
            ops.append({"op": "add", "path": path,
                        "content": "\n".join(content_lines)})
        elif line.startswith(DELETE):
            path = line[len(DELETE):].strip()
            ops.append({"op": "delete", "path": path})
            i += 1
        elif line.startswith(UPDATE):
            path = line[len(UPDATE):].strip()
            i += 1
            hunks: list[dict[str, list[str]]] = []
            cur_hunk: dict[str, list[str]] | None = None
            while i - 1 < len(body) and not body[i - 1].startswith("*** "):
                cur = body[i - 1]
                if cur.strip() == HUNK_SEP:
                    cur_hunk = {"context_del": [], "add": []}
                    hunks.append(cur_hunk)
                    i += 1
                    continue
                if cur_hunk is None:
                    cur_hunk = {"context_del": [], "add": []}
                    hunks.append(cur_hunk)
                if cur.startswith("+"):
                    cur_hunk["add"].append(cur[1:])
                elif cur.startswith("-"):
                    cur_hunk["context_del"].append(cur[1:])
                else:
                    # 上下文行：既参与定位，也保留在替换结果中
                    ctx = cur[1:] if cur.startswith(" ") else cur
                    cur_hunk["context_del"].append(ctx)
                    cur_hunk["add"].append(ctx)
                i += 1
            if not hunks:
                raise PatchError(f"Update File '{path}' has no hunks")
            ops.append({"op": "update", "path": path, "hunks": hunks})
        elif not line.strip():
            i += 1  # 跳过空行
        else:
            raise PatchError(f"Unexpected line in patch: {line!r}")

    if not ops:
        raise PatchError("Patch contains no operations")
    return ops


# ── 应用 ────────────────────────────────────────────────────────────────

def _apply_update(original: str, hunks: list[dict[str, list[str]]]) -> str:
    """对单个文件内容应用所有 update hunks，返回新内容。"""
    content = original
    for h in hunks:
        old_block = "\n".join(h["context_del"])
        new_block = "\n".join(h["add"])
        if old_block == "":
            raise PatchError("Update hunk has no removable/context lines")
        count = content.count(old_block)
        if count == 0:
            raise PatchError(
                f"Hunk context not found:\n{old_block[:200]}"
            )
        if count > 1:
            raise PatchError(
                f"Hunk context is ambiguous (matches {count} places); "
                f"add more context lines:\n{old_block[:200]}"
            )
        content = content.replace(old_block, new_block, 1)
    return content


def apply_patch(text: str, root: str | Path = ".") -> str:
    """
    解析并应用补丁。先全部校验（含磁盘状态），再统一落盘；
    任一步失败则不写任何文件。返回人类可读的变更摘要。
    """
    root = Path(root)
    ops = parse_patch(text)

    # 第一阶段：校验 + 计算每个文件的最终内容（不落盘）
    planned: list[tuple[str, Path, str | None]] = []  # (op, path, new_content|None)
    for op in ops:
        path = root / op["path"]
        if op["op"] == "add":
            if path.exists():
                raise PatchError(f"Add File: '{op['path']}' already exists")
            planned.append(("add", path, op["content"]))
        elif op["op"] == "delete":
            if not path.exists():
                raise PatchError(f"Delete File: '{op['path']}' does not exist")
            planned.append(("delete", path, None))
        elif op["op"] == "update":
            if not path.exists():
                raise PatchError(f"Update File: '{op['path']}' does not exist")
            original = path.read_text(encoding="utf-8")
            new_content = _apply_update(original, op["hunks"])
            planned.append(("update", path, new_content))

    # 第二阶段：落盘
    summary: list[str] = []
    warnings: list[str] = []
    for kind, path, new_content in planned:
        if kind == "add":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new_content or "", encoding="utf-8")
            summary.append(f"  added   {path}")
        elif kind == "delete":
            path.unlink()
            summary.append(f"  deleted {path}")
        elif kind == "update":
            path.write_text(new_content or "", encoding="utf-8")
            summary.append(f"  updated {path}")
        # 对新增/修改的 .py 文件做语法校验
        if kind in ("add", "update"):
            from .file_ops import _syntax_warning
            w = _syntax_warning(str(path), new_content or "")
            if w:
                warnings.append(w)
            # 写后自动格式化（非阻塞）
            from ..core.formatter import format_file
            fmt = format_file(str(path))
            if fmt:
                summary.append("  " + fmt.strip())

    return "Applied patch:\n" + "\n".join(summary) + "".join(warnings)


# ── 工具封装 ──────────────────────────────────────────────────────────────

class ApplyPatchTool(Tool):
    """一次性对多个文件做原子补丁（新增/修改/删除）。"""

    @property
    def name(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return (
            "Apply a multi-file patch atomically (add/update/delete files in one "
            "call). All hunks are validated before anything is written; if any "
            "fails, no file is changed. Prefer this over file_edit for changes "
            "spanning multiple locations or files.\n\n"
            "Format:\n"
            "*** Begin Patch\n"
            "*** Add File: path/new.py\n"
            "+content line\n"
            "*** Update File: path/existing.py\n"
            "@@\n"
            " context line\n"
            "-removed line\n"
            "+added line\n"
            "*** Delete File: path/gone.py\n"
            "*** End Patch"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "The full patch text, from *** Begin Patch to *** End Patch",
                },
                "root": {
                    "type": "string",
                    "description": "Root directory paths are relative to (default: current dir)",
                },
            },
            "required": ["patch"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    async def execute(self, **kwargs: Any) -> str:
        patch = kwargs.get("patch")
        root = kwargs.get("root", ".")
        if not patch:
            return "Error: 'patch' is required"
        try:
            return apply_patch(patch, root=root)
        except PatchError as e:
            return f"Error applying patch: {e}"
        except Exception as e:  # noqa: BLE001 - surface unexpected failures to model
            return f"Error applying patch: {e}"


def register_patch_tools(registry: Any = None) -> None:
    """注册 apply_patch 工具。"""
    from .registry import get_registry

    reg = registry or get_registry()
    reg.register(ApplyPatchTool())
