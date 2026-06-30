"""
测试 skills 发现与加载（渐进式披露）。

全部用临时目录，无模型调用、无网络。
"""
from coding_agent.core.skills import (
    discover_skills,
    load_skill,
    render_available_skills,
    render_skill_content,
    skill_bundled_files,
    _parse_frontmatter,
    _safe_name,
)


def _make_skill(root, name, description="", body="Do the thing.", fm_name=None):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fmn = fm_name if fm_name is not None else name
    (d / "SKILL.md").write_text(
        f"---\nname: {fmn}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return d


def test_parse_frontmatter_basic():
    fm, body = _parse_frontmatter('---\nname: x\ndescription: hi\n---\n\nbody here')
    assert fm["name"] == "x" and fm["description"] == "hi"
    assert body.strip() == "body here"


def test_parse_frontmatter_none():
    fm, body = _parse_frontmatter("no frontmatter here")
    assert fm == {} and body == "no frontmatter here"


def test_parse_frontmatter_strips_quotes():
    fm, _ = _parse_frontmatter('---\nname: "quoted"\n---\nx')
    assert fm["name"] == "quoted"


def test_safe_name_rejects_traversal():
    assert not _safe_name("../evil")
    assert not _safe_name("a/b")
    assert not _safe_name("..")
    assert not _safe_name(".hidden")
    assert _safe_name("deploy")


def test_discover_project_skills(tmp_path):
    proj = tmp_path / "proj"
    skills_root = proj / ".coding-agent" / "skills"
    _make_skill(skills_root, "deploy", "Deploy the app")
    found = discover_skills(cwd=proj, home=tmp_path / "nohome")
    assert "deploy" in found
    assert found["deploy"].description == "Deploy the app"
    assert "Do the thing." in found["deploy"].content


def test_name_defaults_to_dirname(tmp_path):
    proj = tmp_path / "proj"
    root = proj / ".coding-agent" / "skills"
    # frontmatter 无 name 字段 → 用目录名
    d = root / "mytool"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: d\n---\nbody", encoding="utf-8")
    found = discover_skills(cwd=proj, home=tmp_path / "nohome")
    assert "mytool" in found


def test_project_overrides_claude_home(tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    _make_skill(home / ".claude" / "skills", "review", "from claude home")
    _make_skill(proj / ".coding-agent" / "skills", "review", "from project")
    found = discover_skills(cwd=proj, home=home)
    # 项目级优先
    assert found["review"].description == "from project"


def test_claude_home_skills_discovered(tmp_path):
    home = tmp_path / "home"
    _make_skill(home / ".claude" / "skills", "interop", "claude code skill")
    found = discover_skills(cwd=tmp_path / "empty", home=home)
    assert "interop" in found


def test_render_available_skills_lists_names(tmp_path):
    proj = tmp_path / "proj"
    _make_skill(proj / ".coding-agent" / "skills", "deploy", "Ship it")
    found = discover_skills(cwd=proj, home=tmp_path / "h")
    rendered = render_available_skills(found)
    assert "deploy" in rendered and "Ship it" in rendered
    assert "<available_skills>" in rendered


def test_render_available_skills_empty():
    assert render_available_skills({}) == ""


def test_load_skill_by_name(tmp_path):
    proj = tmp_path / "proj"
    _make_skill(proj / ".coding-agent" / "skills", "deploy", "Ship")
    info = load_skill("deploy", cwd=proj, home=tmp_path / "h")
    assert info is not None and info.name == "deploy"


def test_load_skill_traversal_returns_none(tmp_path):
    assert load_skill("../etc", cwd=tmp_path, home=tmp_path) is None


def test_bundled_files_listed(tmp_path):
    proj = tmp_path / "proj"
    d = _make_skill(proj / ".coding-agent" / "skills", "deploy", "Ship")
    (d / "script.sh").write_text("echo hi", encoding="utf-8")
    (d / "reference").mkdir()
    (d / "reference" / "guide.md").write_text("guide", encoding="utf-8")
    info = load_skill("deploy", cwd=proj, home=tmp_path / "h")
    files = skill_bundled_files(info)
    assert any(f.endswith("script.sh") for f in files)
    assert any(f.endswith("guide.md") for f in files)
    assert not any(f.endswith("SKILL.md") for f in files)


def test_render_skill_content_wraps(tmp_path):
    proj = tmp_path / "proj"
    _make_skill(proj / ".coding-agent" / "skills", "deploy", "Ship", body="Step 1.")
    info = load_skill("deploy", cwd=proj, home=tmp_path / "h")
    out = render_skill_content(info, ["/abs/script.sh"])
    assert "Step 1." in out
    assert "Base directory" in out
    assert "/abs/script.sh" in out
    assert out.startswith('<skill_content name="deploy">')
