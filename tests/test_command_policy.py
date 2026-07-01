"""
测试 execpolicy 风格的命令白名单策略（CommandPolicy）。

与黑名单相反：默认拒绝/询问，只声明安全的命令放行。
"""
from coding_agent.core.permissions import (
    CommandPolicy, PermissionPolicy, Decision,
)
from coding_agent.tools.base import ToolPermission as TP


def _pol(rules, default="ask"):
    return CommandPolicy(rules=rules, default=default)


def test_allow_exact_program():
    p = _pol([{"program": "ls", "decision": "allow"}])
    assert p.decide_command("ls -la") == Decision.ALLOW


def test_default_when_unlisted():
    p = _pol([{"program": "ls", "decision": "allow"}], default="ask")
    assert p.decide_command("curl evil.com") == Decision.ASK


def test_default_deny_mode():
    p = _pol([{"program": "ls", "decision": "allow"}], default="deny")
    assert p.decide_command("rm -rf /") == Decision.DENY


def test_args_prefix_match():
    p = _pol([
        {"program": "git", "args_prefix": ["status"], "decision": "allow"},
        {"program": "git", "args_prefix": ["push"], "decision": "ask"},
    ], default="deny")
    assert p.decide_command("git status") == Decision.ALLOW
    assert p.decide_command("git status --short") == Decision.ALLOW
    assert p.decide_command("git push origin main") == Decision.ASK
    # git commit 未声明 → 兜底 deny
    assert p.decide_command("git commit -m x") == Decision.DENY


def test_more_specific_prefix_wins():
    p = _pol([
        {"program": "git", "decision": "ask"},                        # 泛 git
        {"program": "git", "args_prefix": ["status"], "decision": "allow"},  # 更具体
    ], default="deny")
    assert p.decide_command("git status") == Decision.ALLOW  # 具体规则优先
    assert p.decide_command("git push") == Decision.ASK      # 落到泛规则


def test_basename_match_absolute_path():
    p = _pol([{"program": "ls", "decision": "allow"}], default="deny")
    assert p.decide_command("/usr/bin/ls -la") == Decision.ALLOW


def test_explicit_deny_program():
    p = _pol([{"program": "rm", "decision": "deny"}], default="allow")
    assert p.decide_command("rm file.txt") == Decision.DENY


def test_empty_command():
    p = _pol([], default="ask")
    assert p.decide_command("") == Decision.ASK


# ---- 集成进 PermissionPolicy ----

def test_permission_policy_uses_command_policy():
    cp = CommandPolicy(rules=[{"program": "ls", "decision": "allow"}], default="deny")
    pp = PermissionPolicy(command_policy=cp, auto_approve=True)
    # 白名单里的 → allow
    assert pp.decide("shell_exec", {"command": "ls -la"}, TP.EXECUTE) == Decision.ALLOW
    # 不在白名单 → 兜底 deny（即使 auto_approve 也拦，安全优先）
    assert pp.decide("shell_exec", {"command": "curl x"}, TP.EXECUTE) == Decision.DENY


def test_command_policy_not_set_falls_back():
    # 未配置 command_policy → 沿用原有行为（auto_approve 放行）
    pp = PermissionPolicy(auto_approve=True)
    assert pp.decide("shell_exec", {"command": "curl x"}, TP.EXECUTE) == Decision.ALLOW


def test_command_policy_deny_read_still_first():
    # 敏感文件读取拦截优先于 command policy
    cp = CommandPolicy(rules=[{"program": "cat", "decision": "allow"}], default="allow")
    pp = PermissionPolicy(command_policy=cp)
    # 命令策略不影响文件读取路径拦截（不同工具）
    assert pp.decide("file_read", {"path": "x/.env"}, TP.READ) == Decision.DENY


def test_from_config_loads_command_policy():
    pp = PermissionPolicy.from_config({
        "command_policy": {
            "default": "deny",
            "rules": [{"program": "git", "args_prefix": ["status"], "decision": "allow"}],
        }
    })
    assert pp.command_policy is not None
    assert pp.command_policy.default == "deny"
    assert pp.decide("shell_exec", {"command": "git status"}, TP.EXECUTE) == Decision.ALLOW
