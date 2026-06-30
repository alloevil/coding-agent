"""
Git 操作工具

参考 Claude Code 的 git 集成：
- status: 查看状态
- diff: 查看差异
- commit: 提交
- log: 查看日志
"""
from __future__ import annotations

from typing import Any

from .base import Tool, ToolPermission, ToolExecutionError


class GitStatusTool(Tool):
    """查看 git 状态"""
    
    @property
    def name(self) -> str:
        return "git_status"
    
    @property
    def description(self) -> str:
        return "Show the working tree status."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": []
        }
    
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ
    
    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                return f"Error: {stderr.decode('utf-8', errors='replace')}"
            
            return stdout.decode("utf-8", errors="replace")
        except Exception as e:
            return f"Error getting git status: {str(e)}"


class GitDiffTool(Tool):
    """查看 git 差异"""
    
    @property
    def name(self) -> str:
        return "git_diff"
    
    @property
    def description(self) -> str:
        return "Show changes between commits, commit and working tree, etc."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Specific file to diff (optional)"
                },
                "staged": {
                    "type": "boolean",
                    "description": "Show staged changes (default: false)"
                }
            },
            "required": []
        }
    
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ
    
    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        
        path = kwargs.get("path")
        staged = kwargs.get("staged", False)
        
        try:
            cmd = ["git", "diff"]
            if staged:
                cmd.append("--staged")
            if path:
                cmd.extend(["--", path])
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                return f"Error: {stderr.decode('utf-8', errors='replace')}"
            
            output = stdout.decode("utf-8", errors="replace")
            if not output:
                return "No changes"
            
            return output
        except Exception as e:
            return f"Error getting git diff: {str(e)}"


class GitCommitTool(Tool):
    """提交更改"""
    
    @property
    def name(self) -> str:
        return "git_commit"
    
    @property
    def description(self) -> str:
        return "Commit staged changes with a message."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Commit message"
                },
                "add_all": {
                    "type": "boolean",
                    "description": "Stage all changes before committing (default: false)"
                }
            },
            "required": ["message"]
        }
    
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE
    
    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        
        message = kwargs.get("message")
        add_all = kwargs.get("add_all", False)
        
        if not message:
            raise ToolExecutionError(self.name, "message is required")
        
        try:
            # 如果需要，先 add all
            if add_all:
                process = await asyncio.create_subprocess_exec(
                    "git", "add", "-A",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                _, stderr = await process.communicate()
                if process.returncode != 0:
                    return f"Error staging files: {stderr.decode('utf-8', errors='replace')}"
            
            # 提交
            process = await asyncio.create_subprocess_exec(
                "git", "commit", "-m", message,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                return f"Error committing: {stderr.decode('utf-8', errors='replace')}"
            
            return stdout.decode("utf-8", errors="replace")
        except Exception as e:
            return f"Error committing: {str(e)}"


class GitLogTool(Tool):
    """查看 git 日志"""
    
    @property
    def name(self) -> str:
        return "git_log"
    
    @property
    def description(self) -> str:
        return "Show commit logs."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of commits to show (default: 10)"
                },
                "oneline": {
                    "type": "boolean",
                    "description": "Show one line per commit (default: true)"
                }
            },
            "required": []
        }
    
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ
    
    async def execute(self, **kwargs: Any) -> str:
        import asyncio
        
        limit = kwargs.get("limit", 10)
        oneline = kwargs.get("oneline", True)
        
        try:
            cmd = ["git", "log", f"-{limit}"]
            if oneline:
                cmd.append("--oneline")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                return f"Error: {stderr.decode('utf-8', errors='replace')}"
            
            return stdout.decode("utf-8", errors="replace")
        except Exception as e:
            return f"Error getting git log: {str(e)}"


class GitBranchTool(Tool):
    """查看 / 创建 / 切换 git 分支"""

    @property
    def name(self) -> str:
        return "git_branch"

    @property
    def description(self) -> str:
        return (
            "Show, create, or switch git branches. With no arguments, lists local "
            "branches and marks the current one. Pass 'create' to make a new branch "
            "(and switch to it), or 'switch' to checkout an existing branch."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "create": {
                    "type": "string",
                    "description": "Name of a new branch to create and switch to.",
                },
                "switch": {
                    "type": "string",
                    "description": "Name of an existing branch to checkout.",
                },
            },
            "required": [],
        }

    @property
    def permission(self) -> ToolPermission:
        # 列出分支是只读的；创建/切换会改变工作区 → WRITE。
        # 这里取较高级别，确保创建/切换会走权限确认。
        return ToolPermission.WRITE

    async def execute(self, **kwargs: Any) -> str:
        import asyncio

        create = kwargs.get("create")
        switch = kwargs.get("switch")

        async def _run(*cmd: str) -> tuple[int, str, str]:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            return (
                proc.returncode or 0,
                out.decode("utf-8", errors="replace"),
                err.decode("utf-8", errors="replace"),
            )

        try:
            if create:
                code, out, err = await _run("git", "checkout", "-b", create)
                if code != 0:
                    return f"Error creating branch: {err.strip() or out.strip()}"
                return f"Created and switched to branch '{create}'."
            if switch:
                code, out, err = await _run("git", "checkout", switch)
                if code != 0:
                    return f"Error switching branch: {err.strip() or out.strip()}"
                return f"Switched to branch '{switch}'."
            # 默认：列出本地分支
            code, out, err = await _run("git", "branch")
            if code != 0:
                return f"Error: {err.strip() or out.strip()}"
            return out.strip() or "(no branches yet)"
        except Exception as e:  # noqa: BLE001
            return f"Error running git branch: {e}"


def register_git_tools(registry: Any = None) -> None:
    """注册所有 git 工具"""
    from .registry import get_registry

    reg = registry or get_registry()
    reg.register(GitStatusTool())
    reg.register(GitDiffTool())
    reg.register(GitCommitTool())
    reg.register(GitLogTool())
    reg.register(GitBranchTool())
