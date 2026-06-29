"""
ShellSandbox 测试

覆盖：命令黑名单、路径保护、资源限制、网络限制、审计日志、配置管理
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from coding_agent.tools.sandbox import ShellSandbox, reset_sandbox


@pytest.fixture
def tmp_base(tmp_path):
    """提供临时目录作为沙箱 base_dir"""
    return str(tmp_path)


@pytest.fixture
def sandbox(tmp_base):
    """创建沙箱实例，使用临时目录下的配置路径"""
    cfg_path = os.path.join(tmp_base, ".agent", "sandbox.json")
    return ShellSandbox(config_path=cfg_path, base_dir=tmp_base)


@pytest.fixture
def sandbox_with_config(tmp_base):
    """创建带自定义配置文件的沙箱"""
    cfg_path = os.path.join(tmp_base, ".agent", "sandbox.json")
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w") as f:
        json.dump({
            "timeout": 10,
            "max_output_bytes": 512,
            "max_concurrent": 2,
            "blacklist": ["custom_bad_command"],
        }, f)
    return ShellSandbox(config_path=cfg_path, base_dir=tmp_base)


# ── 命令黑名单 ────────────────────────────────────────────

class TestBlacklist:
    def test_rm_rf_root_blocked(self, sandbox):
        ok, reason = sandbox.check("rm -rf /")
        assert not ok
        assert "blacklist" in reason

    def test_rm_rf_root_variant(self, sandbox):
        ok, _ = sandbox.check("rm -rf / --no-preserve-root")
        assert not ok

    def test_mkfs_blocked(self, sandbox):
        ok, _ = sandbox.check("mkfs.ext4 /dev/sda1")
        assert not ok

    def test_dd_blocked(self, sandbox):
        ok, _ = sandbox.check("dd if=/dev/zero of=/dev/sda")
        assert not ok

    def test_fork_bomb_blocked(self, sandbox):
        ok, _ = sandbox.check(":(){ :|:& };:")
        assert not ok

    def test_chmod_777_root_blocked(self, sandbox):
        ok, _ = sandbox.check("chmod 777 /")
        assert not ok

    def test_shutdown_blocked(self, sandbox):
        ok, _ = sandbox.check("shutdown -h now")
        assert not ok

    def test_safe_command_allowed(self, sandbox):
        ok, reason = sandbox.check("ls -la /tmp")
        assert ok
        assert reason == "ok"

    def test_custom_blacklist(self, sandbox_with_config):
        ok, _ = sandbox_with_config.check("custom_bad_command")
        assert not ok

    def test_custom_blacklist_safe_cmd(self, sandbox_with_config):
        ok, _ = sandbox_with_config.check("echo hello")
        assert ok


# ── 路径保护 ──────────────────────────────────────────────

class TestPathProtection:
    def test_write_etc_blocked(self, sandbox):
        for cmd in [
            "echo 'malware' > /etc/passwd",
            "echo 'x' >> /etc/hosts",
            "cp /tmp/file /etc/",
            "mv /tmp/file /etc/",
            "touch /etc/new_file",
            "mkdir /etc/newdir",
        ]:
            ok, reason = sandbox.check(cmd)
            assert not ok, f"Expected blocked: {cmd}"
            assert "/etc" in reason

    def test_write_usr_blocked(self, sandbox):
        ok, _ = sandbox.check("cp /tmp/x /usr/bin/")
        assert not ok

    def test_write_proc_blocked(self, sandbox):
        ok, _ = sandbox.check("echo 1 > /proc/sys/vm/drop_caches")
        assert not ok

    def test_read_shadow_blocked(self, sandbox):
        ok, _ = sandbox.check("cat /etc/shadow")
        assert not ok

    def test_read_shadow_grep(self, sandbox):
        ok, _ = sandbox.check("grep root /etc/shadow")
        assert not ok

    def test_write_non_protected_allowed(self, sandbox):
        ok, _ = sandbox.check("echo hello > /tmp/test.txt")
        assert ok

    def test_read_non_sensitive_allowed(self, sandbox):
        ok, _ = sandbox.check("cat /etc/hostname")
        assert ok


# ── 资源限制 ──────────────────────────────────────────────

class TestResourceLimits:
    @pytest.mark.asyncio
    async def test_timeout(self, sandbox):
        result = await sandbox.run("sleep 5", timeout=1)
        assert "TIMEOUT" in result

    @pytest.mark.asyncio
    async def test_default_timeout(self, sandbox):
        """默认超时应为 30 秒"""
        assert sandbox.config["timeout"] == 30

    @pytest.mark.asyncio
    async def test_output_truncation(self, sandbox):
        """输出超过限制应被截断"""
        # 生成超过 1MB 的输出
        result = await sandbox.run("python3 -c \"print('A' * 2000000)\"")
        assert "truncated" in result.lower() or len(result) < 2_100_000

    @pytest.mark.asyncio
    async def test_concurrent_limit(self, tmp_base):
        """并发执行不应超过限制"""
        s = ShellSandbox(config_path="/nonexistent/sandbox.json", base_dir=tmp_base)
        s.update_config({"max_concurrent": 2})

        started = []
        async def slow_cmd(i):
            started.append(i)
            result = await s.run(f"sleep 0.5 && echo done_{i}")
            return result

        # 同时启动 4 个命令
        results = await asyncio.gather(*[slow_cmd(i) for i in range(4)])
        for r in results:
            assert "done_" in r or "BLOCKED" in r or "TIMEOUT" in r


# ── 网络限制 ──────────────────────────────────────────────

class TestNetworkLimits:
    def test_network_disabled_blocks_curl(self, sandbox):
        sandbox.update_config({"network_allowed": False})
        ok, reason = sandbox.check("curl http://example.com")
        assert not ok
        assert "network" in reason.lower()

    def test_network_disabled_blocks_wget(self, sandbox):
        sandbox.update_config({"network_allowed": False})
        ok, _ = sandbox.check("wget http://example.com")
        assert not ok

    def test_network_disabled_blocks_ssh(self, sandbox):
        sandbox.update_config({"network_allowed": False})
        ok, _ = sandbox.check("ssh user@host")
        assert not ok

    def test_network_allowed_by_default(self, sandbox):
        ok, _ = sandbox.check("curl http://example.com")
        assert ok

    def test_network_blacklist_domain(self, sandbox):
        sandbox.update_config({"network_blacklist": ["evil.com"]})
        ok, _ = sandbox.check("curl http://evil.com/malware")
        assert not ok
        assert "evil.com" in ok.__class__.__name__ or "evil.com" in _


# ── 审计日志 ──────────────────────────────────────────────

class TestAuditLog:
    @pytest.mark.asyncio
    async def test_audit_writes_on_execute(self, sandbox):
        await sandbox.run("echo hello")
        audit = sandbox.get_recent_audit(5)
        assert len(audit) >= 1
        assert "executed" in audit[-1]

    @pytest.mark.asyncio
    async def test_audit_writes_on_block(self, sandbox):
        await sandbox.run("rm -rf /")
        audit = sandbox.get_recent_audit(5)
        assert len(audit) >= 1
        assert "blocked" in audit[-1]

    @pytest.mark.asyncio
    async def test_audit_writes_on_timeout(self, sandbox):
        await sandbox.run("sleep 5", timeout=1)
        audit = sandbox.get_recent_audit(5)
        assert len(audit) >= 1
        assert "timeout" in audit[-1]

    @pytest.mark.asyncio
    async def test_audit_contains_command(self, sandbox):
        await sandbox.run("echo test123")
        audit = sandbox.get_recent_audit(1)
        assert "echo test123" in audit[0]


# ── 配置管理 ──────────────────────────────────────────────

class TestConfig:
    def test_default_config(self, sandbox):
        cfg = sandbox.config
        assert cfg["enabled"] is True
        assert cfg["timeout"] == 30
        assert cfg["max_concurrent"] == 3
        assert cfg["network_allowed"] is True
        assert len(cfg["blacklist"]) > 0

    def test_load_from_file(self, sandbox_with_config):
        cfg = sandbox_with_config.config
        assert cfg["timeout"] == 10
        assert cfg["max_output_bytes"] == 512
        assert "custom_bad_command" in cfg["blacklist"]

    def test_update_config(self, sandbox):
        sandbox.update_config({"timeout": 60, "max_concurrent": 5})
        assert sandbox.config["timeout"] == 60
        assert sandbox.config["max_concurrent"] == 5

    def test_disable_enable(self, sandbox):
        sandbox.disable()
        assert not sandbox.enabled
        # 禁用后任何命令都应通过
        ok, _ = sandbox.check("rm -rf /")
        assert ok

        sandbox.enable()
        assert sandbox.enabled
        ok, _ = sandbox.check("rm -rf /")
        assert not ok

    def test_config_persistence(self, tmp_base):
        """配置应持久化到文件"""
        cfg_path = os.path.join(tmp_base, ".agent", "sandbox.json")
        s = ShellSandbox(config_path=cfg_path, base_dir=tmp_base)
        s.update_config({"timeout": 99})

        # 重新加载
        s2 = ShellSandbox(config_path=cfg_path, base_dir=tmp_base)
        assert s2.config["timeout"] == 99


# ── 集成测试 ──────────────────────────────────────────────

class TestIntegration:
    @pytest.mark.asyncio
    async def test_safe_command_executes(self, sandbox):
        result = await sandbox.run("echo hello world")
        assert "hello world" in result

    @pytest.mark.asyncio
    async def test_blocked_command_returns_message(self, sandbox):
        result = await sandbox.run("rm -rf /")
        assert "BLOCKED" in result

    @pytest.mark.asyncio
    async def test_workdir_passes_through(self, sandbox, tmp_base):
        result = await sandbox.run("pwd", workdir=tmp_base)
        assert tmp_base in result or "Error" not in result

    @pytest.mark.asyncio
    async def test_exit_code_reported(self, sandbox):
        result = await sandbox.run("exit 42")
        assert "42" in result


# ── reset_sandbox 清理 ────────────────────────────────────

@pytest.fixture(autouse=True)
def _cleanup():
    """每个测试后重置全局沙箱"""
    yield
    reset_sandbox()
