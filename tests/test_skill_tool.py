"""
测试 `skill` 工具：加载、未知名、路径穿越。
"""
import asyncio

from coding_agent.tools.skill_ops import SkillTool, register_skill_tools


def _make_skill(root, name, description="", body="Step 1. Do it."):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return d


def test_skill_tool_loads(tmp_path):
    proj = tmp_path / "proj"
    _make_skill(proj / ".coding-agent" / "skills", "deploy", "Ship the app",
                body="Run make deploy.")
    tool = SkillTool(cwd=str(proj), home=str(tmp_path / "h"))
    out = asyncio.run(tool.execute(name="deploy"))
    assert "Run make deploy." in out
    assert 'name="deploy"' in out


def test_skill_tool_unknown_name_lists_available(tmp_path):
    proj = tmp_path / "proj"
    _make_skill(proj / ".coding-agent" / "skills", "deploy", "Ship")
    tool = SkillTool(cwd=str(proj), home=str(tmp_path / "h"))
    out = asyncio.run(tool.execute(name="nope"))
    assert out.lower().startswith("error")
    assert "deploy" in out  # 提示可用列表


def test_skill_tool_no_skills_message(tmp_path):
    tool = SkillTool(cwd=str(tmp_path / "empty"), home=str(tmp_path / "h"))
    out = asyncio.run(tool.execute(name="x"))
    assert "no skills" in out.lower()


def test_skill_tool_traversal_rejected(tmp_path):
    tool = SkillTool(cwd=str(tmp_path), home=str(tmp_path))
    out = asyncio.run(tool.execute(name="../../etc/passwd"))
    assert out.lower().startswith("error")


def test_skill_tool_missing_name(tmp_path):
    tool = SkillTool(cwd=str(tmp_path), home=str(tmp_path))
    out = asyncio.run(tool.execute(name=""))
    assert out.lower().startswith("error")


def test_skill_tool_bundles_files(tmp_path):
    proj = tmp_path / "proj"
    d = _make_skill(proj / ".coding-agent" / "skills", "deploy", "Ship")
    (d / "deploy.sh").write_text("echo go", encoding="utf-8")
    tool = SkillTool(cwd=str(proj), home=str(tmp_path / "h"))
    out = asyncio.run(tool.execute(name="deploy"))
    assert "deploy.sh" in out
    assert "<skill_files>" in out


def test_register(tmp_path):
    from coding_agent.tools.registry import ToolRegistry
    reg = ToolRegistry()
    t = register_skill_tools(reg, cwd=str(tmp_path), home=str(tmp_path))
    assert reg.get_tool("skill") is not None
    assert isinstance(t, SkillTool)
