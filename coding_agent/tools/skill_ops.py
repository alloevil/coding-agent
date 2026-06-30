"""
`skill` 工具 - 按需加载一个 skill 的完整指令（渐进式披露的"展开"半步）

模型在上下文里看到 <available_skills> 清单（仅 name+description）；当某个任务与
某 skill 匹配时，调用本工具按名加载其 SKILL.md 正文与捆绑文件清单。

参考 opencode 的 skill 工具（开源）：拒绝未知名/路径穿越；返回包裹在
<skill_content> 中的正文，并附基目录与文件清单，便于模型继续读取脚本/参考资料。
"""
from __future__ import annotations

from typing import Any

from .base import Tool, ToolPermission
from ..core.skills import (
    load_skill,
    render_skill_content,
    skill_bundled_files,
    discover_skills,
)


class SkillTool(Tool):
    """加载一个已发现的 skill 的完整指令。"""

    def __init__(self, cwd: str | None = None, home: str | None = None):
        # 允许注入根目录，便于测试；默认运行时用 cwd / $HOME。
        self._cwd = cwd
        self._home = home

    @property
    def name(self) -> str:
        return "skill"

    @property
    def description(self) -> str:
        return (
            "Load the full instructions for a named skill from available_skills. "
            "Call this when the current task matches a skill's description, BEFORE "
            "doing the work — the returned content gives you the detailed procedure "
            "and any bundled scripts/reference files for that skill."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill name, exactly as listed in available_skills.",
                }
            },
            "required": ["name"],
        }

    @property
    def permission(self) -> ToolPermission:
        # 只读取磁盘上的指令文件，无副作用。
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return "Error: 'name' is required."
        info = load_skill(name, cwd=self._cwd, home=self._home)
        if info is None:
            available = sorted(discover_skills(cwd=self._cwd, home=self._home))
            hint = (f" Available: {', '.join(available)}." if available
                    else " No skills are installed.")
            return f"Error: skill '{name}' not found.{hint}"
        files = skill_bundled_files(info)
        return render_skill_content(info, files)


def register_skill_tools(registry: Any = None, cwd: str | None = None,
                         home: str | None = None) -> SkillTool:
    """注册 skill 工具，返回该实例（调用方可用于发现可用 skills）。"""
    from .registry import get_registry

    reg = registry or get_registry()
    tool = SkillTool(cwd=cwd, home=home)
    reg.register(tool)
    return tool
