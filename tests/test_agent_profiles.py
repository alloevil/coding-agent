"""
测试命名 agent profile 的发现与加载。
"""
from coding_agent.core.agent_profiles import (
    discover_agents,
    load_agent,
    render_available_agents,
    AgentProfile,
    _parse_list,
)


def _make_agent(root, name, frontmatter, body="You are a helpful agent."):
    d = root / ".coding-agent" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    fm_lines = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
    (d / f"{name}.md").write_text(f"---\n{fm_lines}\n---\n\n{body}\n", encoding="utf-8")
    return d


def test_parse_list():
    assert _parse_list("a, b ,c") == ["a", "b", "c"]
    assert _parse_list("") == []
    assert _parse_list("solo") == ["solo"]


def test_discover_basic(tmp_path):
    _make_agent(tmp_path, "reviewer",
                {"description": "Code reviewer", "model": "gpt-5-mini",
                 "mode": "subagent", "temperature": "0.2"},
                body="Review carefully.")
    agents = discover_agents(cwd=tmp_path, home=tmp_path / "h")
    a = agents["reviewer"]
    assert a.description == "Code reviewer"
    assert a.model == "gpt-5-mini"
    assert a.mode == "subagent"
    assert a.temperature == 0.2
    assert a.system_prompt == "Review carefully."


def test_name_defaults_to_filename(tmp_path):
    d = tmp_path / ".coding-agent" / "agents"
    d.mkdir(parents=True)
    (d / "fixer.md").write_text("---\ndescription: d\n---\nbody", encoding="utf-8")
    agents = discover_agents(cwd=tmp_path, home=tmp_path / "h")
    assert "fixer" in agents


def test_invalid_mode_falls_back_primary(tmp_path):
    _make_agent(tmp_path, "x", {"mode": "weird"})
    agents = discover_agents(cwd=tmp_path, home=tmp_path / "h")
    assert agents["x"].mode == "primary"


def test_tool_allow_filter(tmp_path):
    _make_agent(tmp_path, "ro", {"tools": "file_read, grep, file_search"})
    a = load_agent("ro", cwd=tmp_path, home=tmp_path / "h")
    assert a.tool_allowed("file_read") is True
    assert a.tool_allowed("file_write") is False  # 不在白名单


def test_tool_deny_filter(tmp_path):
    _make_agent(tmp_path, "safe", {"deny_tools": "file_write, shell_exec"})
    a = load_agent("safe", cwd=tmp_path, home=tmp_path / "h")
    assert a.tool_allowed("file_read") is True
    assert a.tool_allowed("file_write") is False  # 黑名单


def test_no_filter_allows_all(tmp_path):
    _make_agent(tmp_path, "open", {})
    a = load_agent("open", cwd=tmp_path, home=tmp_path / "h")
    assert a.tool_allowed("file_write") is True


def test_project_overrides_global(tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    # global
    gd = home / ".config" / "coding-agent" / "agents"
    gd.mkdir(parents=True)
    (gd / "rev.md").write_text("---\ndescription: from global\n---\nx", encoding="utf-8")
    # project
    _make_agent(proj, "rev", {"description": "from project"})
    agents = discover_agents(cwd=proj, home=home)
    assert agents["rev"].description == "from project"


def test_load_traversal_returns_none(tmp_path):
    assert load_agent("../etc", cwd=tmp_path, home=tmp_path) is None


def test_render_available_lists(tmp_path):
    _make_agent(tmp_path, "reviewer", {"description": "Reviews code", "mode": "subagent"})
    agents = discover_agents(cwd=tmp_path, home=tmp_path / "h")
    out = render_available_agents(agents)
    assert "reviewer" in out and "Reviews code" in out


def test_render_empty():
    assert "No custom agents" in render_available_agents({})
