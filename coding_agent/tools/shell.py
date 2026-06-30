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
        # 持久化工作目录：cd 在多次 shell_exec 调用之间保留
        # （None 表示进程默认 cwd）。参考交互式 shell 的行为。
        self._cwd: str | None = None
    
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

        # 决定本次执行的工作目录：显式 workdir 优先，否则用持久化的 _cwd
        from pathlib import Path
        effective_cwd = workdir or self._cwd

        # Auto-install dependencies if requirements.txt exists
        cmd_parts = command.strip().split()
        is_python_cmd = cmd_parts and cmd_parts[0] in ('python', 'python3', 'py')
        if is_python_cmd:
            req_path = Path(effective_cwd or ".") / "requirements.txt"
            if req_path.exists():
                try:
                    install_cmd = f"pip install -r {req_path} -q"
                    await self.sandbox.run(install_cmd, workdir=effective_cwd, timeout=60)
                except Exception:
                    pass  # Continue even if install fails

        # 包裹命令：执行后打印最终 $PWD，以便把 cd 的效果持久化到下次调用。
        # 用一个不太可能与正常输出冲突的哨兵标记分隔。
        sentinel = "__CWD_AFTER__:"
        wrapped = f"{command}\n__rc=$?; printf '\\n{sentinel}%s' \"$PWD\"; exit $__rc"

        result = await self.sandbox.run(wrapped, workdir=effective_cwd, timeout=timeout)

        # 解析尾部的 cwd 哨兵，更新持久 cwd，并从展示输出里剥离
        new_cwd = self._extract_and_strip_cwd(result, sentinel)
        if new_cwd is not None:
            self._cwd = new_cwd
            return self._strip_sentinel(result, sentinel)
        return result

    def _extract_and_strip_cwd(self, output: str, sentinel: str) -> str | None:
        """从输出末尾解析 cwd 哨兵，返回新 cwd（解析不到返回 None）。"""
        idx = output.rfind(sentinel)
        if idx == -1:
            return None
        tail = output[idx + len(sentinel):].strip()
        # 哨兵后应是一个绝对路径
        if tail.startswith("/"):
            # 只取第一行（防御截断/多余输出）
            return tail.splitlines()[0].strip()
        return None

    def _strip_sentinel(self, output: str, sentinel: str) -> str:
        """从展示输出里移除 cwd 哨兵行。"""
        idx = output.rfind(sentinel)
        if idx == -1:
            return output
        # 哨兵前我们额外加了一个 '\n'，一并去掉
        cleaned = output[:idx]
        if cleaned.endswith("\n"):
            cleaned = cleaned[:-1]
        return cleaned


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


def register_shell_tools(registry: Any = None) -> None:
    """注册 shell 和沙箱工具"""
    from .registry import get_registry

    reg = registry or get_registry()
    sandbox = get_sandbox()
    reg.register(ShellExecTool(sandbox))
    reg.register(SandboxStatusTool(sandbox))
    reg.register(SandboxConfigureTool(sandbox))
