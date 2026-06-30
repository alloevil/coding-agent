"""
Shell 安全沙箱

提供命令执行的安全限制：
- 命令黑名单拦截
- 路径保护（禁止写/读系统关键目录和文件）
- 资源限制（超时、输出大小、并发数）
- 网络限制（可选）
- 审计日志
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import Tool, ToolPermission, ToolExecutionError

logger = logging.getLogger(__name__)

# ── 默认配置 ──────────────────────────────────────────────

_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "timeout": 30,
    "max_output_bytes": 1_048_576,  # 1 MB
    "max_concurrent": 3,
    "audit_log": ".agent/audit.log",
    "blacklist": [
        r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?(-[a-zA-Z]*r[a-zA-Z]*\s+)?/",
        r"\brm\s+-rf\s+/",
        r"\bmkfs\b",
        r"\bdd\s+",
        r":\(\)\s*\{\s*:\|:&\s*\};:",
        r"\bchmod\s+777\s+/",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\binit\s+0",
        r"\bmkswap\b",
        r"\bswapoff\b",
        r"\bmodprobe\s+-r\b",
        r">\s*/dev/sd[a-z]",
    ],
    "protected_write_paths": [
        "/etc", "/usr", "/bin", "/sbin", "/boot",
        "/proc", "/sys", "/dev", "/lib", "/lib64",
    ],
    "protected_read_files": [
        "/etc/shadow",
        "/etc/gshadow",
        "/etc/master.passwd",
    ],
    "network_allowed": True,
    "network_whitelist": [],  # empty = all allowed when network_allowed=True
    "network_blacklist": [],  # domains/IPs to block
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典，override 优先"""
    merged = dict(base)
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


@dataclass
class AuditEntry:
    """审计日志条目"""
    timestamp: float
    command: str
    result: str  # "executed" | "blocked" | "failed" | "timeout"
    detail: str = ""
    workdir: str | None = None


class ShellSandbox:
    """
    Shell 执行安全沙箱

    使用方式：
        sandbox = ShellSandbox()
        ok, reason = sandbox.check("ls -la")        # 预检
        if ok:
            result = await sandbox.run(cmd, workdir, timeout)  # 受限执行
        sandbox.disable()                            # 需要 DANGEROUS 权限
    """

    def __init__(self, config_path: str | None = None, base_dir: str | None = None):
        self._base_dir = Path(base_dir) if base_dir else Path.cwd()
        self._config_path = config_path or str(self._base_dir / ".agent" / "sandbox.json")
        self._config: dict[str, Any] = dict(_DEFAULT_CONFIG)
        self._semaphore = asyncio.Semaphore(self._config["max_concurrent"])
        self._audit_log_path: str | None = None
        self._audit_lock = asyncio.Lock()

        self._load_config()
        self._ensure_audit_log()

    # ── 配置管理 ──────────────────────────────────────────

    def _load_config(self) -> None:
        """从 JSON 文件加载配置（可选）"""
        path = Path(self._config_path)
        if path.exists():
            try:
                with open(path) as f:
                    user_cfg = json.load(f)
                self._config = _deep_merge(_DEFAULT_CONFIG, user_cfg)
                self._semaphore = asyncio.Semaphore(self._config["max_concurrent"])
                logger.info("Sandbox config loaded from %s", path)
            except Exception as e:
                logger.warning("Failed to load sandbox config from %s: %s", path, e)

    def save_config(self) -> None:
        """保存当前配置到 JSON 文件"""
        path = Path(self._config_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
        except OSError as e:
            logger.warning("Failed to save sandbox config to %s: %s", path, e)

    def update_config(self, updates: dict[str, Any]) -> None:
        """更新配置（部分更新）"""
        self._config = _deep_merge(self._config, updates)
        self._semaphore = asyncio.Semaphore(self._config["max_concurrent"])
        self.save_config()

    @property
    def config(self) -> dict[str, Any]:
        return dict(self._config)

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled", True))

    def disable(self) -> None:
        """禁用沙箱（需要 DANGEROUS 权限）"""
        self._config["enabled"] = False
        self.save_config()

    def enable(self) -> None:
        """重新启用沙箱"""
        self._config["enabled"] = True
        self.save_config()

    # ── 审计日志 ──────────────────────────────────────────

    def _ensure_audit_log(self) -> None:
        log_path = self._config.get("audit_log", ".agent/audit.log")
        if not os.path.isabs(log_path):
            log_path = str(self._base_dir / log_path)
        self._audit_log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

    async def _write_audit(self, entry: AuditEntry) -> None:
        """写入审计日志（追加模式）"""
        async with self._audit_lock:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.timestamp))
            line = (
                f"{ts} | {entry.result:>8} | "
                f"cmd={entry.command!r}"
            )
            if entry.workdir:
                line += f" | cwd={entry.workdir!r}"
            if entry.detail:
                line += f" | {entry.detail}"
            line += "\n"
            try:
                with open(self._audit_log_path, "a") as f:
                    f.write(line)
            except Exception as e:
                logger.warning("Failed to write audit log: %s", e)

    def get_recent_audit(self, lines: int = 20) -> list[str]:
        """读取最近的审计日志"""
        if not self._audit_log_path or not os.path.exists(self._audit_log_path):
            return []
        try:
            with open(self._audit_log_path) as f:
                all_lines = f.readlines()
            return [l.rstrip("\n") for l in all_lines[-lines:]]
        except Exception:
            return []

    # ── 安全检查 ──────────────────────────────────────────

    def check(self, command: str, workdir: str | None = None) -> tuple[bool, str]:
        """
        预检命令安全性

        Returns:
            (allowed: bool, reason: str)
        """
        if not self.enabled:
            return True, "sandbox disabled"

        # 1. 命令黑名单
        for pattern in self._config.get("blacklist", []):
            if re.search(pattern, command, re.IGNORECASE):
                return False, f"blocked by blacklist pattern: {pattern}"

        # 2. 路径保护 - 写入检查
        for protected in self._config.get("protected_write_paths", []):
            # 匹配任何形式的写入：>, >>, tee, cp ... /protected, mv ... /protected,
            # mkdir -p /protected, touch /protected, etc.
            write_patterns = [
                rf">\s*{re.escape(protected)}",          # > /etc/...
                rf">>\s*{re.escape(protected)}",         # >> /etc/...
                rf"\btee\s+{re.escape(protected)}",      # tee /etc/...
                rf"\bcp\s+\S+\s+{re.escape(protected)}", # cp file /etc/...
                rf"\bmv\s+\S+\s+{re.escape(protected)}", # mv file /etc/...
                rf"\brm\s+.*{re.escape(protected)}",      # rm /etc/...
                rf"\bmkdir\s+.*{re.escape(protected)}",   # mkdir /etc/...
                rf"\btouch\s+.*{re.escape(protected)}",   # touch /etc/...
                rf"\bchmod\s+\S+\s+{re.escape(protected)}",  # chmod xxx /etc/...
                rf"\bchown\s+\S+\s+{re.escape(protected)}",  # chown xxx /etc/...
                rf"\bsed\s+-i.*{re.escape(protected)}",   # sed -i ... /etc/...
                rf"\brm\s+-[a-zA-Z]*\s+{re.escape(protected)}",
            ]
            for wp in write_patterns:
                if re.search(wp, command):
                    return False, f"blocked: write to protected path {protected}"

        # 3. 路径保护 - 读取检查（敏感文件）
        for protected_file in self._config.get("protected_read_files", []):
            if re.search(re.escape(protected_file), command):
                # 只阻止直接读取（cat, less, more, head, tail, vi, nano, grep 等）
                read_patterns = [
                    rf"\bcat\s+.*{re.escape(protected_file)}",
                    rf"\bless\s+.*{re.escape(protected_file)}",
                    rf"\bmore\s+.*{re.escape(protected_file)}",
                    rf"\bhead\s+.*{re.escape(protected_file)}",
                    rf"\btail\s+.*{re.escape(protected_file)}",
                    rf"\bgrep\s+.*{re.escape(protected_file)}",
                    rf"\bawk\s+.*{re.escape(protected_file)}",
                    rf"\bsed\s+.*{re.escape(protected_file)}",
                    rf"\bnano\s+.*{re.escape(protected_file)}",
                    rf"\bvi\s+.*{re.escape(protected_file)}",
                    rf"\bvim\s+.*{re.escape(protected_file)}",
                    rf"<\s*{re.escape(protected_file)}",
                ]
                for rp in read_patterns:
                    if re.search(rp, command):
                        return False, f"blocked: read of protected file {protected_file}"

        # 4. 网络限制（可选）
        if not self._config.get("network_allowed", True):
            # 检查常见网络工具
            net_patterns = [
                r"\bcurl\b", r"\bwget\b", r"\bnc\b", r"\bncat\b",
                r"\bnmap\b", r"\bssh\b", r"\bscp\b", r"\brsync\b",
                r"\bftp\b", r"\bsftp\b", r"\btelnet\b",
            ]
            for np in net_patterns:
                if re.search(np, command):
                    return False, "blocked: network access disabled"

        # 网络黑名单域名
        for domain in self._config.get("network_blacklist", []):
            if domain in command:
                return False, f"blocked: domain {domain} is blacklisted"

        return True, "ok"

    # ── 受限执行 ──────────────────────────────────────────

    async def run(
        self,
        command: str,
        workdir: str | None = None,
        timeout: int | None = None,
    ) -> str:
        """
        通过沙箱执行命令

        流程：预检 → 并发限制 → 超时 → 输出截断 → 审计
        """
        # 预检
        allowed, reason = self.check(command, workdir)
        if not allowed:
            await self._write_audit(AuditEntry(
                timestamp=time.time(), command=command,
                result="blocked", detail=reason, workdir=workdir,
            ))
            return f"🚫 BLOCKED: {reason}"

        # 并发限制
        effective_timeout = timeout or self._config.get("timeout", 30)
        max_output = self._config.get("max_output_bytes", 1_048_576)

        async with self._semaphore:
            try:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=effective_timeout,
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    await self._write_audit(AuditEntry(
                        timestamp=time.time(), command=command,
                        result="timeout",
                        detail=f"timeout after {effective_timeout}s",
                        workdir=workdir,
                    ))
                    return f"⏱ TIMEOUT: Command timed out after {effective_timeout} seconds"

                # 截断输出
                stdout_text = stdout.decode("utf-8", errors="replace")
                stderr_text = stderr.decode("utf-8", errors="replace")

                truncated = False
                if len(stdout) > max_output:
                    stdout_text = stdout[:max_output].decode("utf-8", errors="replace") + "\n... [truncated]"
                    truncated = True
                if len(stderr) > max_output:
                    stderr_text = stderr[:max_output].decode("utf-8", errors="replace") + "\n... [truncated]"
                    truncated = True

                result_parts = []
                if process.returncode != 0:
                    # 显式失败横幅，放在最前面，确保模型一眼看到失败（不被埋没）
                    result_parts.append(f"❌ Command failed (exit code {process.returncode})")
                if stdout_text:
                    result_parts.append(f"stdout:\n{stdout_text}")
                if stderr_text:
                    result_parts.append(f"stderr:\n{stderr_text}")
                if process.returncode != 0:
                    result_parts.append(f"exit code: {process.returncode}")
                if truncated:
                    result_parts.append("[output truncated]")

                result_str = "\n\n".join(result_parts) if result_parts else "Command executed successfully (no output)"

                await self._write_audit(AuditEntry(
                    timestamp=time.time(), command=command,
                    result="executed",
                    detail=f"rc={process.returncode}" + (" truncated" if truncated else ""),
                    workdir=workdir,
                ))
                return result_str

            except Exception as e:
                await self._write_audit(AuditEntry(
                    timestamp=time.time(), command=command,
                    result="failed", detail=str(e), workdir=workdir,
                ))
                return f"Error executing command: {e}"


# ── 全局单例 ──────────────────────────────────────────────

_global_sandbox: ShellSandbox | None = None


def get_sandbox(base_dir: str | None = None) -> ShellSandbox:
    """获取全局沙箱实例"""
    global _global_sandbox
    if _global_sandbox is None:
        _global_sandbox = ShellSandbox(base_dir=base_dir)
    return _global_sandbox


def reset_sandbox() -> None:
    """重置全局沙箱（测试用）"""
    global _global_sandbox
    _global_sandbox = None
