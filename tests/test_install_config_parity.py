"""
测试安装/配置对齐 Codex 的三项：
- updater.discover_install_dir / run_update（自更新）
- config.apply_cli_overrides（-c 命令行覆盖）
- trust（信任目录）
"""
import json

import pytest

from coding_agent.core.config import AgentConfig, apply_cli_overrides
from coding_agent.core import trust as T
from coding_agent.core import updater as U


# ── -c 命令行覆盖 ────────────────────────────────────────────────────

def test_override_applies_typed_values():
    c = AgentConfig(model="gpt-4", protocol="openai", max_turns=100,
                    auto_approve=False)
    apply_cli_overrides(c, ["model=claude-opus-4-8", "protocol=anthropic",
                            "max_turns=3", "auto_approve=yes"])
    assert c.model == "claude-opus-4-8"
    assert c.protocol == "anthropic"
    assert c.max_turns == 3 and isinstance(c.max_turns, int)
    assert c.auto_approve is True


def test_override_temperature_none():
    c = AgentConfig()
    apply_cli_overrides(c, ["temperature=none"])
    assert c.temperature is None


def test_override_unknown_key_raises():
    with pytest.raises(ValueError, match="unknown config key"):
        apply_cli_overrides(AgentConfig(), ["bogus=1"])


def test_override_missing_equals_raises():
    with pytest.raises(ValueError, match="expected KEY=VALUE"):
        apply_cli_overrides(AgentConfig(), ["model"])


def test_override_bad_int_raises():
    with pytest.raises(ValueError, match="invalid value"):
        apply_cli_overrides(AgentConfig(), ["max_turns=notanumber"])


def test_override_empty_list_noop():
    c = AgentConfig(model="m")
    apply_cli_overrides(c, [])
    assert c.model == "m"


# ── trust directory ─────────────────────────────────────────────────

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "cfg"
    monkeypatch.setenv("CODING_AGENT_HOME", str(h))
    return str(h)


def test_untrusted_by_default(home, tmp_path):
    assert T.is_trusted(str(tmp_path), home=home) is False


def test_trust_then_is_trusted(home, tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    T.trust_directory(str(d), home=home)
    assert T.is_trusted(str(d), home=home) is True


def test_trust_covers_subdirectory(home, tmp_path):
    d = tmp_path / "proj"
    (d / "sub").mkdir(parents=True)
    T.trust_directory(str(d), home=home)
    assert T.is_trusted(str(d / "sub"), home=home) is True


def test_trust_persists_to_config_json(home, tmp_path):
    d = tmp_path / "proj"; d.mkdir()
    T.trust_directory(str(d), home=home)
    from coding_agent.core.setup_wizard import global_config_path
    data = json.loads(global_config_path(home).read_text())
    assert "trusted_dirs" in data
    assert len(data["trusted_dirs"]) == 1


def test_trust_merges_not_clobbers(home, tmp_path):
    # 已有别的配置键时，trust 不应覆盖它们
    from coding_agent.core.setup_wizard import global_config_path
    p = global_config_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"api_key": "sk-x", "model": "m"}))
    d = tmp_path / "proj"; d.mkdir()
    T.trust_directory(str(d), home=home)
    data = json.loads(p.read_text())
    assert data["api_key"] == "sk-x"       # 原键保留
    assert data["model"] == "m"
    assert len(data["trusted_dirs"]) == 1


def test_trust_idempotent(home, tmp_path):
    d = tmp_path / "proj"; d.mkdir()
    T.trust_directory(str(d), home=home)
    T.trust_directory(str(d), home=home)  # 二次不应重复
    assert len(T.list_trusted(home=home)) == 1


# ── updater ─────────────────────────────────────────────────────────

def test_discover_install_dir_finds_repo():
    # 本仓库有 pyproject.toml，应能被探测到
    d = U.discover_install_dir()
    assert d is not None
    assert (d / "pyproject.toml").is_file()


def test_discover_uses_env(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setenv("CODING_AGENT_DIR", str(tmp_path))
    assert U.discover_install_dir() == tmp_path


def test_run_update_no_install_dir_reports(monkeypatch):
    monkeypatch.setattr(U, "discover_install_dir", lambda: None)
    out = []
    code = U.run_update(install_dir=None, out=out.append)
    assert code == 1
    assert any("Can't find" in line for line in out)


def test_run_update_non_git_skips_pull(monkeypatch, tmp_path):
    # 有 pyproject 但非 git 仓库 → 跳过 pull，pip 步骤用 stub
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    calls = []

    def fake_run(cmd, cwd):
        calls.append(cmd)
        return True, "ok"

    monkeypatch.setattr(U, "_run", fake_run)
    out = []
    code = U.run_update(install_dir=tmp_path, out=out.append)
    assert code == 0
    # 没有 git pull（非 git 仓库）
    assert not any(c[:2] == ["git", "pull"] for c in calls)
    assert any("skipping `git pull`" in line for line in out)
