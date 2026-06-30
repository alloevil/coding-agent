"""
配置驱动的命令 hook - 在生命周期事件上运行 shell 命令

参考 Claude Code 的 settings.json hooks：用户在配置里为某个事件登记一组
shell 命令，事件触发时由 harness 执行它们（而不是模型）。

配置形如：
  "hooks": {
    "pre_tool_use":  [{"command": "echo pre >> /tmp/log"}],
    "post_tool_use": [{"command": "ruff check ."}],
    "on_compact":    [{"command": "..."}]
  }

事件名 → HookEvent 的映射见 _EVENT_MAP。命令在后台同步执行（短超时），
其 stdout/退出码不影响主流程（pre_tool_use 返回非零可阻断工具——预留）。
"""
from __future__ import annotations

import subprocess
from typing import Any

from ..tools.base import HookEvent, HookContext

_EVENT_MAP = {
    "pre_tool_use": HookEvent.PRE_TOOL_USE,
    "post_tool_use": HookEvent.POST_TOOL_USE,
    "pre_model_call": HookEvent.PRE_MODEL_CALL,
    "post_model_call": HookEvent.POST_MODEL_CALL,
    "on_error": HookEvent.ON_ERROR,
    "on_compact": HookEvent.ON_COMPACT,
}


def _make_command_hook(command: str, timeout: int = 30, block_on_failure: bool = False):
    """构造一个运行 shell 命令的 hook。返回 True 表示阻断操作。"""
    def hook(ctx: HookContext) -> bool | None:
        import os
        env = {**os.environ,
               "CODING_AGENT_HOOK_EVENT": ctx.event.value,
               "CODING_AGENT_TOOL_NAME": ctx.tool_name or ""}
        try:
            result = subprocess.run(
                command, shell=True, timeout=timeout,
                capture_output=True, text=True, env=env,
            )
        except Exception:
            return None  # hook 自身出错不影响主流程
        if block_on_failure and result.returncode != 0:
            return True
        return None
    return hook


def register_config_hooks(hooks_config: dict[str, Any], registry: Any) -> int:
    """
    把配置里的命令 hook 登记到 registry。返回登记的 hook 数量。

    每条配置项：{"command": str, "timeout"?: int, "block"?: bool}
    """
    count = 0
    for event_name, entries in (hooks_config or {}).items():
        event = _EVENT_MAP.get(event_name)
        if event is None or not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                entry = {"command": entry}
            if not isinstance(entry, dict) or "command" not in entry:
                continue
            registry.add_hook(event, _make_command_hook(
                entry["command"],
                timeout=int(entry.get("timeout", 30)),
                block_on_failure=bool(entry.get("block", False)),
            ))
            count += 1
    return count
