"""
文件操作工具

参考 Claude Code 的文件工具：
- file_read: 读取文件
- file_write: 写入文件
- file_edit: 精确编辑（基于 oldText/newText）
- file_search: 搜索文件（glob）
- grep: 内容搜索
"""
from __future__ import annotations

import os
import glob as glob_module
from pathlib import Path
from typing import Any

from .base import Tool, ToolPermission, ToolExecutionError

# 搜索时默认跳过的噪音目录（VCS、依赖、构建产物、虚拟环境等）。
# 真实仓库里递归这些目录既慢又会淹没结果。
DEFAULT_IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "env", ".tox",
    "dist", "build", ".next", ".nuxt", "target", ".gradle",
    ".idea", ".vscode", ".cache",
})


def _syntax_warning(path: str, content: str) -> str:
    """
    对刚写入的文件做尽力而为的语法校验，返回一段警告后缀（无问题则空串）。

    当前支持 Python（ast.parse）。设计为非阻塞：写入照常完成，只是在
    引入语法错误时附加提示，让模型有机会立刻修复，而不是等运行时才发现。
    """
    if not path.endswith(".py"):
        return ""
    import ast
    try:
        ast.parse(content)
        return ""
    except SyntaxError as e:
        return (
            f"\n⚠️ Warning: '{path}' has a Python syntax error after this write "
            f"(line {e.lineno}: {e.msg}). The write succeeded, but you likely "
            f"need to fix it."
        )


def _is_ignored(path: Path, ignore_dirs: frozenset[str]) -> bool:
    """路径中是否包含任一被忽略的目录名。"""
    return any(part in ignore_dirs for part in path.parts)


def _iter_files(root: Path, include: str | None, ignore_dirs: frozenset[str]):
    """递归产出文件，跳过被忽略的目录。"""
    pattern = include or "*"
    for p in root.rglob(pattern):
        if not p.is_file():
            continue
        if _is_ignored(p.relative_to(root) if p.is_relative_to(root) else p, ignore_dirs):
            continue
        yield p


class FileReadTool(Tool):
    """读取文件"""
    
    @property
    def name(self) -> str:
        return "file_read"
    
    @property
    def description(self) -> str:
        return "Read the contents of a file. Returns the file content as text."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read"
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read"
                }
            },
            "required": ["path"]
        }
    
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ
    
    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")
        offset = kwargs.get("offset", 1)
        limit = kwargs.get("limit")

        if not path:
            raise ToolExecutionError(self.name, "path is required")

        try:
            file_path = Path(path)
            if not file_path.exists():
                return f"Error: File '{path}' does not exist"
            if file_path.is_dir():
                return f"Error: '{path}' is a directory, not a file"

            # 大文件保护：超过 5MB 时拒绝整文件读取，提示使用 offset/limit
            MAX_BYTES = 5 * 1024 * 1024
            size = file_path.stat().st_size
            if size > MAX_BYTES and not limit:
                return (
                    f"Error: File '{path}' is {size // 1024} KB, too large to read "
                    f"in full. Pass 'offset' and 'limit' to read a slice, or use grep."
                )

            # 二进制文件保护：检测 NUL 字节
            with open(file_path, "rb") as fb:
                head = fb.read(8192)
            if b"\x00" in head:
                return f"Error: '{path}' appears to be a binary file; cannot display as text"

            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            total_lines = len(all_lines)
            start = max(0, offset - 1)  # 转换为 0-indexed

            # 默认分页：未指定 limit 时，单次最多返回 DEFAULT_PAGE 行，
            # 并在末尾告知如何读取下一页（避免一次性灌入超长文件）。
            DEFAULT_PAGE = 2000
            page = limit if limit else DEFAULT_PAGE
            lines = all_lines[start:start + page]
            end = start + len(lines)  # 0-indexed exclusive

            # 添加行号
            result = []
            for i, line in enumerate(lines, start=start + 1):
                result.append(f"{i:4d} | {line.rstrip()}")
            body = "\n".join(result)

            # 分页脚注：还有更多内容时提示下一页参数
            if end < total_lines:
                remaining = total_lines - end
                body += (
                    f"\n\n... {remaining} more line(s). "
                    f"Read next page with offset={end + 1}"
                    + (f", limit={page}." if limit else ".")
                )
            return body
        except Exception as e:
            return f"Error reading file: {str(e)}"


class FileWriteTool(Tool):
    """写入文件"""
    
    @property
    def name(self) -> str:
        return "file_write"
    
    @property
    def description(self) -> str:
        return "Write content to a file. Creates the file if it doesn't exist, overwrites if it does."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write"
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file"
                }
            },
            "required": ["path", "content"]
        }
    
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE
    
    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")
        content = kwargs.get("content")
        
        if not path:
            raise ToolExecutionError(self.name, "path is required")
        if content is None:
            raise ToolExecutionError(self.name, "content is required")
        
        try:
            file_path = Path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            
            return f"Successfully wrote {len(content)} bytes to '{path}'" + _syntax_warning(path, content)
        except Exception as e:
            return f"Error writing file: {str(e)}"


class FileEditTool(Tool):
    """
    精确编辑文件
    
    参考 Claude Code 的编辑方式：
    - 基于 oldText/newText 的精确替换
    - oldText 必须唯一匹配
    """
    
    @property
    def name(self) -> str:
        return "file_edit"
    
    @property
    def description(self) -> str:
        return "Edit a file by replacing exact text. The old_text must be unique in the file."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit"
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to find and replace (must be unique unless replace_all=true)"
                },
                "new_text": {
                    "type": "string",
                    "description": "New text to replace with"
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence instead of requiring a unique match (default false)"
                }
            },
            "required": ["path", "old_text", "new_text"]
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")
        old_text = kwargs.get("old_text")
        new_text = kwargs.get("new_text")
        replace_all = kwargs.get("replace_all", False)

        if not path:
            raise ToolExecutionError(self.name, "path is required")
        if old_text is None:
            raise ToolExecutionError(self.name, "old_text is required")
        if new_text is None:
            raise ToolExecutionError(self.name, "new_text is required")
        if old_text == new_text:
            return "Error: old_text and new_text are identical; nothing to do"

        try:
            file_path = Path(path)
            if not file_path.exists():
                return f"Error: File '{path}' does not exist"

            content = file_path.read_text(encoding="utf-8")

            # 多策略匹配：精确 → 行级 strip → 空白归一 → 缩进无关 → 块锚点。
            # 比精确匹配更鲁棒（容忍空白/缩进漂移），同时保持唯一性约束。
            from ..core.text_replace import fuzzy_replace, ReplaceError
            try:
                new_content = fuzzy_replace(content, old_text, new_text, replace_all=replace_all)
            except ReplaceError as e:
                msg = str(e)
                # 多处匹配时附带行号提示，帮助模型消歧
                if "multiple matches" in msg:
                    first = old_text.splitlines()[0] if old_text.splitlines() else old_text
                    line_nums = [ln for ln, line in enumerate(content.splitlines(), 1)
                                 if first.strip() and first.strip() in line]
                    hint = f" (near lines {line_nums[:10]})" if line_nums else ""
                    return f"Error: {msg}{hint}"
                return f"Error: {msg}"

            file_path.write_text(new_content, encoding="utf-8")
            suffix = " (replace_all)" if replace_all else ""
            return f"Successfully edited '{path}'{suffix}" + _syntax_warning(path, new_content)
        except Exception as e:
            return f"Error editing file: {str(e)}"


class FileSearchTool(Tool):
    """搜索文件（glob）"""
    
    @property
    def name(self) -> str:
        return "file_search"
    
    @property
    def description(self) -> str:
        return "Search for files matching a glob pattern."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match (e.g., '**/*.py', 'src/**/*.ts')"
                },
                "root": {
                    "type": "string",
                    "description": "Root directory to search from (default: current directory)"
                }
            },
            "required": ["pattern"]
        }
    
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ
    
    async def execute(self, **kwargs: Any) -> str:
        pattern = kwargs.get("pattern")
        root = kwargs.get("root", ".")
        
        if not pattern:
            raise ToolExecutionError(self.name, "pattern is required")
        
        try:
            full_pattern = os.path.join(root, pattern)
            matches = glob_module.glob(full_pattern, recursive=True)

            # 过滤掉噪音目录中的匹配
            matches = [
                m for m in matches
                if not _is_ignored(Path(m), DEFAULT_IGNORE_DIRS)
            ]

            if not matches:
                return f"No files found matching '{pattern}'"
            
            # 限制输出数量
            if len(matches) > 100:
                matches = matches[:100]
                result = f"Found {len(matches)}+ files (showing first 100):\n"
            else:
                result = f"Found {len(matches)} files:\n"
            
            for match in sorted(matches):
                result += f"  {match}\n"
            
            return result.rstrip()
        except Exception as e:
            return f"Error searching files: {str(e)}"


class GrepTool(Tool):
    """内容搜索"""
    
    @property
    def name(self) -> str:
        return "grep"
    
    @property
    def description(self) -> str:
        return "Search for text content in files."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text or regex pattern to search for"
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default: current directory)"
                },
                "include": {
                    "type": "string",
                    "description": "File pattern to include (e.g., '*.py')"
                },
                "include_ignored": {
                    "type": "boolean",
                    "description": "Also search ignored dirs (.git, node_modules, venv, build...). Default false."
                }
            },
            "required": ["pattern"]
        }
    
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ
    
    async def execute(self, **kwargs: Any) -> str:
        import re

        pattern = kwargs.get("pattern")
        path = kwargs.get("path", ".")
        include = kwargs.get("include")
        include_ignored = kwargs.get("include_ignored", False)

        if not pattern:
            raise ToolExecutionError(self.name, "pattern is required")

        ignore_dirs = frozenset() if include_ignored else DEFAULT_IGNORE_DIRS

        try:
            results = []
            path_obj = Path(path)

            if path_obj.is_file():
                files = [path_obj]
            else:
                files = list(_iter_files(path_obj, include, ignore_dirs))

            for file_path in files:
                try:
                    content = file_path.read_text(encoding="utf-8")
                    for i, line in enumerate(content.splitlines(), 1):
                        if re.search(pattern, line, re.IGNORECASE):
                            results.append(f"{file_path}:{i}: {line.rstrip()}")
                            if len(results) >= 100:
                                break
                except (UnicodeDecodeError, PermissionError):
                    continue
                
                if len(results) >= 100:
                    break
            
            if not results:
                return f"No matches found for '{pattern}'"
            
            result = f"Found {len(results)} matches:\n" + "\n".join(results)
            if len(results) >= 100:
                result += "\n... (truncated, showing first 100 matches)"
            
            return result
        except Exception as e:
            return f"Error searching: {str(e)}"


class ListFilesTool(Tool):
    """列出目录中的文件"""
    
    @property
    def name(self) -> str:
        return "list_files"
    
    @property
    def description(self) -> str:
        return "List all files in a directory. Useful for verifying what files exist."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list (default: current directory)"
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Whether to list files recursively (default: false)"
                }
            },
            "required": []
        }
    
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ
    
    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path", ".")
        recursive = kwargs.get("recursive", False)
        
        try:
            path_obj = Path(path)
            if not path_obj.exists():
                return f"Error: Directory '{path}' does not exist"
            
            if recursive:
                files = sorted(path_obj.rglob("*"))
            else:
                files = sorted(path_obj.iterdir())
            
            result = []
            for f in files:
                if f.is_file():
                    size = f.stat().st_size
                    result.append(f"📄 {f.name} ({size} bytes)")
                elif f.is_dir():
                    result.append(f"📁 {f.name}/")
            
            if not result:
                return f"Directory '{path}' is empty"
            
            return f"Files in '{path}':\n" + "\n".join(result)
        except Exception as e:
            return f"Error listing files: {str(e)}"


def register_file_tools(registry: Any = None) -> None:
    """注册所有文件操作工具"""
    from .registry import get_registry

    reg = registry or get_registry()
    reg.register(FileReadTool())
    reg.register(FileWriteTool())
    reg.register(FileEditTool())
    reg.register(FileSearchTool())
    reg.register(GrepTool())
    reg.register(ListFilesTool())
