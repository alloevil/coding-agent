"""
测试 slash:true 的 skill 可作为 /name 斜杠命令调用。
"""
from coding_agent.core.skills import discover_skills
from coding_agent.core.commands import load_custom_commands, dispatch, CommandContext


def _skill(root, name, body, slash):
    d = root / ".coding-agent" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    slash_line = "slash: true\n" if slash else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n{slash_line}---\n\n{body}\n",
        encoding="utf-8",
    )


def test_slash_flag_parsed(tmp_path):
    _skill(tmp_path, "review", "Do a review.", slash=True)
    _skill(tmp_path, "deploy", "Ship it.", slash=False)
    found = discover_skills(cwd=tmp_path, home=tmp_path / "h")
    assert found["review"].slash is True
    assert found["deploy"].slash is False


def test_slash_skill_becomes_command(tmp_path):
    _skill(tmp_path, "review", "Review the diff carefully.", slash=True)
    cmds = load_custom_commands(root=tmp_path)
    assert "review" in cmds
    assert "Review the diff carefully." in cmds["review"]


def test_non_slash_skill_not_command(tmp_path):
    _skill(tmp_path, "deploy", "Ship it.", slash=False)
    cmds = load_custom_commands(root=tmp_path)
    assert "deploy" not in cmds


def test_dispatch_slash_skill_as_prompt(tmp_path):
    _skill(tmp_path, "review", "Review carefully. $ARGUMENTS", slash=True)
    cmds = load_custom_commands(root=tmp_path)
    r = dispatch("/review the auth module", CommandContext(tool_names=[]), custom=cmds)
    assert r.kind == "prompt"
    assert "Review carefully." in r.payload
    assert "the auth module" in r.payload  # $ARGUMENTS 替换


def test_command_file_overrides_skill(tmp_path):
    _skill(tmp_path, "review", "from skill", slash=True)
    cmd_dir = tmp_path / ".coding-agent" / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    (cmd_dir / "review.md").write_text("from command file", encoding="utf-8")
    cmds = load_custom_commands(root=tmp_path)
    assert cmds["review"] == "from command file"
