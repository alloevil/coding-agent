"""
测试分层配置解析 AgentConfig.resolve()
"""
import json
import pytest

from coding_agent.core.config import AgentConfig


def _clear_env(monkeypatch):
    for k in ("MODEL_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY",
              "MODEL_BASE_URL", "OPENAI_API_BASE", "LLM_BASE_URL",
              "CODING_AGENT_MODEL", "MODEL_PRIMARY"):
        monkeypatch.delenv(k, raising=False)


def test_resolve_defaults_when_no_files(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nope"))
    cfg = AgentConfig.resolve()
    assert cfg.max_turns == 100  # default


def test_global_config_applied(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    (home).mkdir()
    (home / "config.json").write_text(json.dumps({"max_turns": 7, "temperature": 0.2}))
    monkeypatch.setenv("CODING_AGENT_HOME", str(home))
    cfg = AgentConfig.resolve()
    assert cfg.max_turns == 7
    assert cfg.temperature == 0.2


def test_project_overrides_global(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    home = tmp_path / "home"; home.mkdir()
    (home / "config.json").write_text(json.dumps({"max_turns": 7, "model": "global-model"}))
    proj = tmp_path / "proj"; proj.mkdir()
    (proj / ".coding-agent.json").write_text(json.dumps({"max_turns": 3}))
    monkeypatch.setenv("CODING_AGENT_HOME", str(home))
    monkeypatch.chdir(proj)
    cfg = AgentConfig.resolve()
    assert cfg.max_turns == 3        # project wins
    assert cfg.model == "global-model"  # global still applies where project silent


def test_env_key_wins_over_files(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".coding-agent.json").write_text(json.dumps({"api_key": "from-file", "model": "file-model"}))
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nope"))
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    monkeypatch.setenv("CODING_AGENT_MODEL", "env-model")
    cfg = AgentConfig.resolve()
    assert cfg.api_key == "from-env"   # env wins for secrets
    assert cfg.model == "env-model"


def test_malformed_file_ignored(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".coding-agent.json").write_text("{ not json")
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nope"))
    cfg = AgentConfig.resolve()  # 不应抛异常
    assert cfg.max_turns == 100
