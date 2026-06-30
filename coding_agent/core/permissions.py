"""
细粒度权限策略 - allow / deny / ask 规则引擎

参考 Claude Code 的权限模型：除了按工具的权限级别（READ/WRITE/EXECUTE），
还支持基于 (工具名, 参数) 的规则，决定每次调用是 allow（自动放行）、
deny（直接拒绝）还是 ask（询问用户）。

求值顺序（deny-first，安全优先）：
  1. deny 规则命中 → DENY（最高优先级，任何 allow 都不能覆盖）
  2. allow 规则命中 → ALLOW
  3. 否则回退到默认级别策略（READ 自动放行，其余 ask）

规则用简单的声明式结构表达，便于从配置/JSON 加载：
  - tool: 工具名（"*" 匹配全部）
  - paths: 路径 glob 列表（匹配 arguments 里的 path/file 等字段）
  - commands: 子串/正则列表（匹配 shell_exec 的 command）
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..tools.base import ToolPermission


class Decision(Enum):
    ALLOW = "allow"   # 自动放行，不询问
    DENY = "deny"     # 直接拒绝
    ASK = "ask"       # 询问用户


# arguments 里可能承载路径的字段名
_PATH_KEYS = ("path", "file", "file_path", "filename", "workdir", "root")

# 默认拒绝读取的敏感文件（即使是 READ 工具）
DEFAULT_DENY_READ_PATHS = [
    "**/.env", "**/.env.*", "**/*.pem", "**/*.key",
    "**/id_rsa", "**/id_ed25519", "**/.ssh/**",
    "**/.aws/credentials", "**/.netrc",
]


@dataclass
class Rule:
    """一条权限规则。任一维度为空表示该维度不限制。"""
    tool: str = "*"
    paths: list[str] = field(default_factory=list)      # glob
    commands: list[str] = field(default_factory=list)   # 正则/子串

    def matches(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        if self.tool != "*" and self.tool != tool_name:
            return False
        if self.paths:
            cand = [str(arguments[k]) for k in _PATH_KEYS if k in arguments and arguments[k]]
            if not any(_path_matches(p, pat) for p in cand for pat in self.paths):
                return False
        if self.commands:
            cmd = str(arguments.get("command", ""))
            if not any(re.search(pat, cmd) for pat in self.commands):
                return False
        # tool 匹配且(若指定)path/command 也匹配
        return True


def _path_matches(path: str, pattern: str) -> bool:
    # 同时按完整路径和 basename 匹配，兼容相对/绝对路径
    import os
    return (
        fnmatch.fnmatch(path, pattern)
        or fnmatch.fnmatch(os.path.basename(path), pattern.lstrip("*/"))
        or fnmatch.fnmatch(os.path.normpath(path), pattern)
    )


@dataclass
class PermissionPolicy:
    """
    权限策略：deny 规则 + allow 规则 + 默认级别回退。

    auto_approve=True 时整体放行（但 deny 规则仍然生效，安全兜底）。
    """
    allow_rules: list[Rule] = field(default_factory=list)
    deny_rules: list[Rule] = field(default_factory=list)
    auto_approve: bool = False
    deny_read_paths: list[str] = field(default_factory=lambda: list(DEFAULT_DENY_READ_PATHS))
    # 只读规划模式：只允许 READ 工具 + 下面这些"无副作用交互"工具
    plan_mode: bool = False
    plan_mode_allow: list[str] = field(default_factory=lambda: ["update_plan", "ask_user"])
    # 外部目录守卫：对工作区根之外的写/执行路径要求确认（参考 opencode
    # assertExternalDirectory）。workspace_root=None 表示不限制；
    # allow_external_writes=True 时放行（仅这层；显式 deny 规则仍生效）。
    workspace_root: str | None = None
    allow_external_writes: bool = False

    def _is_external_path(self, path: str) -> bool:
        """判断 path 是否落在 workspace_root 之外（解析符号链接/相对路径后比较）。"""
        import os
        root = self.workspace_root
        if not root:
            return False
        try:
            root_abs = os.path.realpath(root)
            target_abs = os.path.realpath(path)
        except (OSError, ValueError):
            return False
        # target 必须等于 root 或在 root 之下
        try:
            common = os.path.commonpath([root_abs, target_abs])
        except ValueError:
            # 不同盘符（Windows）等 → 视为外部
            return True
        return common != root_abs

    def decide(self, tool_name: str, arguments: dict[str, Any],
               permission: ToolPermission) -> Decision:
        # 0. 规划模式：deny 任何会改变系统的工具（WRITE/EXECUTE/DANGEROUS），
        #    放行 READ 与白名单（update_plan / ask_user）。优先级仅次于敏感读取拦截。
        if self.plan_mode:
            cand = [str(arguments[k]) for k in _PATH_KEYS if k in arguments and arguments[k]]
            for p in cand:
                for pat in self.deny_read_paths:
                    if _path_matches(p, pat):
                        return Decision.DENY
            if tool_name in self.plan_mode_allow or permission == ToolPermission.READ:
                return Decision.ALLOW
            return Decision.DENY

        # 1. 内置敏感读取拦截（deny-first）
        cand = [str(arguments[k]) for k in _PATH_KEYS if k in arguments and arguments[k]]
        for p in cand:
            for pat in self.deny_read_paths:
                if _path_matches(p, pat):
                    return Decision.DENY

        # 2. 显式 deny 规则
        for rule in self.deny_rules:
            if rule.matches(tool_name, arguments):
                return Decision.DENY

        # 2b. 外部目录守卫：工作区根之外的写/执行路径要求确认（ASK），
        #     除非 allow_external_writes。READ 不在此限（敏感读取已在第 1 步拦）。
        if (self.workspace_root and not self.allow_external_writes
                and permission != ToolPermission.READ):
            for p in cand:
                if self._is_external_path(p):
                    return Decision.ASK

        # 3. 显式 allow 规则
        for rule in self.allow_rules:
            if rule.matches(tool_name, arguments):
                return Decision.ALLOW

        # 4. 全局自动放行
        if self.auto_approve:
            return Decision.ALLOW

        # 5. 默认级别策略：READ 放行，其余询问
        if permission == ToolPermission.READ:
            return Decision.ALLOW
        return Decision.ASK

    @classmethod
    def from_config(cls, data: dict[str, Any] | None, auto_approve: bool = False) -> "PermissionPolicy":
        """从配置 dict 构造：{"allow": [...], "deny": [...], "deny_read_paths": [...]}。"""
        data = data or {}

        def _mk(rules: list[Any]) -> list[Rule]:
            out = []
            for r in rules or []:
                if isinstance(r, str):
                    out.append(Rule(tool=r))  # 简写：仅工具名
                elif isinstance(r, dict):
                    out.append(Rule(
                        tool=r.get("tool", "*"),
                        paths=r.get("paths", []),
                        commands=r.get("commands", []),
                    ))
            return out

        policy = cls(
            allow_rules=_mk(data.get("allow", [])),
            deny_rules=_mk(data.get("deny", [])),
            auto_approve=auto_approve,
        )
        if "deny_read_paths" in data:
            policy.deny_read_paths = list(data["deny_read_paths"])
        if "workspace_root" in data:
            policy.workspace_root = data["workspace_root"]
        if "allow_external_writes" in data:
            policy.allow_external_writes = bool(data["allow_external_writes"])
        return policy
