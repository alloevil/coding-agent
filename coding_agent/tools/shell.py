"""
Shell 执行工具

参考 Claude Code 的 shell 工具设计：
- 支持超时
- 支持工作目录
- 捕获 stdout 和 stderr
- 安全沙箱保护
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any

from .base import Tool, ToolPermission, ToolExecutionError
from .sandbox import ShellSandbox, get_sandbox


class ShellExecTool(Tool):
    """执行 shell 命令（经过安全沙箱）"""
    
    def __init__(self, sandbox: ShellSandbox | None = None):
        self._sandbox = sandbox
    
    @property
    def sandbox(self) -> ShellSandbox:
        if self._sandbox is None:
            self._sandbox = get_sandbox()
        return self._sandbox
    
    @property
    def name(self) -> str:
        return "shell_exec"
    
    @property
    def description(self) -> str:
        return (
            "Execute a shell command and return the output. "
            "Commands are checked against a security sandbox before execution."
        )
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute"
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory for the command"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30, max from sandbox config)"
                }
            },
            "required": ["command"]
        }
    
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.EXECUTE
    
    async def execute(self, **kwargs: Any) -> str:
        command = kwargs.get("command")
        workdir = kwargs.get("workdir")
        timeout = kwargs.get("timeout")
        
        if not command:
            raise ToolExecutionError(self.name, "command is required")
        
        # Auto-install dependencies if requirements.txt exists
        import os
        from pathlib import Path
        
        # Check if we're running a Python script
        cmd_parts = command.strip().split()
        is_python_cmd = cmd_parts and cmd_parts[0] in ('python', 'python3', 'py')
        
        if is_python_cmd:
            # Check for requirements.txt in working directory
            work_dir = Path(workdir or ".")
            req_path = work_dir / "requirements.txt"
            
            if req_path.exists():
                # Try to install dependencies first
                try:
                    install_cmd = f"pip install -r {req_path} -q"
                    await self.sandbox.run(install_cmd, workdir=workdir, timeout=60)
                except Exception:
                    pass  # Continue even if install fails
        
        # 通过沙箱执行（含预检、并发限制、超时、输出截断、审计）
        return await self.sandbox.run(command, workdir=workdir, timeout=timeout)


class SandboxStatusTool(Tool):
    """查看沙箱状态和最近的审计日志"""
    
    def __init__(self, sandbox: ShellSandbox | None = None):
        self._sandbox = sandbox
    
    @property
    def sandbox(self) -> ShellSandbox:
        if self._sandbox is None:
            self._sandbox = get_sandbox()
        return self._sandbox
    
    @property
    def name(self) -> str:
        return "sandbox_status"
    
    @property
    def description(self) -> str:
        return "View current sandbox configuration and recent audit log entries."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "integer",
                    "description": "Number of recent audit log lines to show (default: 20)"
                }
            },
        }
    
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ
    
    async def execute(self, **kwargs: Any) -> str:
        lines = kwargs.get("lines", 20)
        
        cfg = self.sandbox.config
        # 脱敏：不暴露完整 blacklist 内容，只显示数量
        display_cfg = dict(cfg)
        for key in ("blacklist", "protected_write_paths", "protected_read_files"):
            if key in display_cfg and isinstance(display_cfg[key], list):
                display_cfg[key] = f"[{len(display_cfg[key])} rules]"
        
        audit = self.sandbox.get_recent_audit(lines)
        
        parts = [
            "📦 Sandbox Status",
            f"  Enabled: {cfg.get('enabled', True)}",
            f"  Timeout: {cfg.get('timeout', 30)}s",
            f"  Max Output: {cfg.get('max_output_bytes', 1_048_576) // 1024}KB",
            f"  Max Concurrent: {cfg.get('max_concurrent', 3)}",
            f"  Network Allowed: {cfg.get('network_allowed', True)}",
            f"  Blacklist Rules: {len(cfg.get('blacklist', []))}",
            f"  Protected Write Paths: {len(cfg.get('protected_write_paths', []))}",
            f"  Protected Read Files: {len(cfg.get('protected_read_files', []))}",
            "",
            f"📜 Recent Audit Log ({len(audit)} entries):",
        ]
        
        if audit:
            parts.extend(f"  {line}" for line in audit)
        else:
            parts.append("  (no entries)")
        
        return "\n".join(parts)


class SandboxConfigureTool(Tool):
    """更新沙箱配置（需要 DANGEROUS 权限）"""
    
    def __init__(self, sandbox: ShellSandbox | None = None):
        self._sandbox = sandbox
    
    @property
    def sandbox(self) -> ShellSandbox:
        if self._sandbox is None:
            self._sandbox = get_sandbox()
        return self._sandbox
    
    @property
    def name(self) -> str:
        return "sandbox_configure"
    
    @property
    def description(self) -> str:
        return (
            "Update sandbox configuration. Requires DANGEROUS permission. "
            "Pass a JSON object with config keys to update. "
            "Use 'enable'/'disable' to toggle the sandbox. "
            "Available keys: enabled, timeout, max_output_bytes, max_concurrent, "
            "network_allowed, network_whitelist, network_blacklist, "
            "blacklist, protected_write_paths, protected_read_files, audit_log."
        )
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "description": "Config updates as a JSON object",
                },
                "enable": {
                    "type": "boolean",
                    "description": "Set to true to enable, false to disable the sandbox",
                },
            },
        }
    
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGEROUS
    
    async def execute(self, **kwargs: Any) -> str:
        config_updates = kwargs.get("config")
        enable_flag = kwargs.get("enable")
        
        if enable_flag is True:
            self.sandbox.enable()
            return "✅ Sandbox enabled"
        
        if enable_flag is False:
            self.sandbox.disable()
            return "⚠️ Sandbox disabled"
        
        if config_updates and isinstance(config_updates, dict):
            self.sandbox.update_config(config_updates)
            return f"✅ Sandbox config updated: {list(config_updates.keys())}"
        
        return "No changes specified. Pass 'config' (JSON object) or 'enable' (bool)."


def register_shell_tools() -> None:
    """注册 shell 和沙箱工具"""
    from .registry import register_tool
    
    sandbox = get_sandbox()
    register_tool(ShellExecTool(sandbox))
    register_tool(SandboxStatusTool(sandbox))
    register_tool(SandboxConfigureTool(sandbox))
