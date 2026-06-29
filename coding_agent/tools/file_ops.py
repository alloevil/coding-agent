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
            
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            # 应用 offset 和 limit
            start = max(0, offset - 1)  # 转换为 0-indexed
            if limit:
                lines = lines[start:start + limit]
            else:
                lines = lines[start:]
            
            # 添加行号
            result = []
            for i, line in enumerate(lines, start=start + 1):
                result.append(f"{i:4d} | {line.rstrip()}")
            
            return "\n".join(result)
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
            
            return f"Successfully wrote {len(content)} bytes to '{path}'"
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
                    "description": "Exact text to find and replace (must be unique)"
                },
                "new_text": {
                    "type": "string",
                    "description": "New text to replace with"
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
        
        if not path:
            raise ToolExecutionError(self.name, "path is required")
        if old_text is None:
            raise ToolExecutionError(self.name, "old_text is required")
        if new_text is None:
            raise ToolExecutionError(self.name, "new_text is required")
        
        try:
            file_path = Path(path)
            if not file_path.exists():
                return f"Error: File '{path}' does not exist"
            
            content = file_path.read_text(encoding="utf-8")
            
            # 检查 old_text 是否存在
            count = content.count(old_text)
            if count == 0:
                return f"Error: old_text not found in '{path}'"
            if count > 1:
                return f"Error: old_text found {count} times in '{path}', must be unique"
            
            # 执行替换
            new_content = content.replace(old_text, new_text, 1)
            file_path.write_text(new_content, encoding="utf-8")
            
            return f"Successfully edited '{path}'"
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
        
        if not pattern:
            raise ToolExecutionError(self.name, "pattern is required")
        
        try:
            results = []
            path_obj = Path(path)
            
            if path_obj.is_file():
                files = [path_obj]
            else:
                # 搜索目录
                if include:
                    files = list(path_obj.rglob(include))
                else:
                    files = list(path_obj.rglob("*"))
                    files = [f for f in files if f.is_file()]
            
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


def register_file_tools() -> None:
    """注册所有文件操作工具"""
    from .registry import register_tool
    
    register_tool(FileReadTool())
    register_tool(FileWriteTool())
    register_tool(FileEditTool())
    register_tool(FileSearchTool())
    register_tool(GrepTool())
    register_tool(ListFilesTool())
