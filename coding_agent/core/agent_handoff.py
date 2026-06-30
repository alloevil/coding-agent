"""
Agent 切换交接 - plan → build 的上下文提醒

参考 opencode 的 session/reminders.ts（开源参考）：当从"规划"切换到"执行"时，
往下一轮上下文注入一段合成提醒，让执行方知道"你刚才在规划，现在去执行计划"，
并在存在计划时提示按计划执行。

设计为纯函数 + 一次性消费：CLI/TUI 在切换时设置一个 pending note，run() 在
下一轮把它作为 system 提醒注入，然后清除。
"""
from __future__ import annotations

from typing import Any


_BUILD_SWITCH = (
    "You have switched out of planning into execution mode. Stop planning and "
    "start making the changes. If a plan was laid out (see the current plan or a "
    "plan file), execute on it step by step, keeping it updated via update_plan."
)


def build_switch_note(had_plan: bool = False) -> str:
    """生成 plan→build 切换提醒文本。"""
    note = _BUILD_SWITCH
    if had_plan:
        note += (" A plan already exists; follow it rather than re-deriving the "
                 "approach from scratch.")
    return note


def should_handoff(prev_agent: str | None, prev_plan_mode: bool,
                   new_agent: str | None, new_plan_mode: bool) -> bool:
    """
    判断是否应触发 plan→build 交接。

    触发条件：从一个"规划态"切到"执行态"。规划态 = 上一个 agent 名为 'plan'
    或上一刻处于 plan_mode；执行态 = 现在既不是 plan agent 也不在 plan_mode。
    """
    was_planning = (prev_agent == "plan") or prev_plan_mode
    now_executing = (new_agent != "plan") and (not new_plan_mode)
    return was_planning and now_executing
