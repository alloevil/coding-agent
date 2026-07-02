"""
编辑日志 — 支持 /undo 撤销最近一次文件改动。

写类工具（file_write / file_edit / apply_patch）在改动**之前**调用 record()
记录文件的先前内容（新建文件记 None）。undo_last() 恢复最近一条：
  - prior 是字符串 → 写回旧内容
  - prior 是 None（原本不存在）→ 删除该文件

LIFO 栈，进程内（一次会话内有效）。纯本地，不依赖 git。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class _Edit:
    path: str
    prior: str | None  # None = 文件原本不存在（undo 时删除）


class EditJournal:
    def __init__(self) -> None:
        self._stack: list[_Edit] = []

    def record(self, path: str) -> None:
        """在写入 path 之前调用：抓当前内容入栈（不存在则记 None）。"""
        p = Path(path)
        try:
            prior = p.read_text(encoding="utf-8") if p.is_file() else None
        except OSError:
            prior = None
        self._stack.append(_Edit(path=str(p), prior=prior))

    def can_undo(self) -> bool:
        return bool(self._stack)

    def undo_last(self) -> str:
        """恢复最近一次改动，返回人类可读结果。空栈返回提示。"""
        if not self._stack:
            return "Nothing to undo."
        edit = self._stack.pop()
        p = Path(edit.path)
        try:
            if edit.prior is None:
                # 原本不存在 → 删除
                if p.is_file():
                    p.unlink()
                return f"↩ Undid creation of {edit.path} (deleted)."
            p.write_text(edit.prior, encoding="utf-8")
            return f"↩ Reverted {edit.path} to its previous contents."
        except OSError as e:
            return f"Undo failed for {edit.path}: {e}"

    def clear(self) -> None:
        self._stack.clear()


# 进程内单例：写工具 record，/undo 消费。
_JOURNAL = EditJournal()


def get_edit_journal() -> EditJournal:
    return _JOURNAL


def reset_edit_journal() -> None:
    _JOURNAL.clear()
