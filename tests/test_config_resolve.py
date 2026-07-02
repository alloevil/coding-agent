"""
测试分层配置解析 AgentConfig.resolve()
"""
import json
import pytest

from coding_agent.core.config import AgentConfig


def _clear_env(monkeypatch):
    for k in ("MODEL_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY",
              "MODEL_BASE_URL", "OPENAI_API_BASE", "LLM_BASE_URL",
              "CODING_AGENT_MODEL", "MODEL_PRIMARY",
              "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
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


def test_anthropic_env_sets_protocol_and_bearer_header(tmp_path, monkeypatch):
    # Rust TUI reads ANTHROPIC_AUTH_TOKEN; the Python layer must too, so the
    # CLI front-end and the "config.json already exists" path behave the same.
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nope"))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-abc")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gw/anthropic")
    monkeypatch.setenv("CODING_AGENT_MODEL", "claude-opus-4-8[1m]")
    cfg = AgentConfig.resolve()
    assert cfg.api_key == "tok-abc"
    assert cfg.protocol == "anthropic"
    assert cfg.api_base_url == "http://gw/anthropic"
    assert cfg.extra_headers.get("Authorization") == "Bearer tok-abc"
    assert cfg.model == "claude-opus-4-8"  # [1m] suffix stripped


def test_anthropic_env_overrides_existing_config_file(tmp_path, monkeypatch):
    # The real bug: config.json already has a key, so the env token used to be
    # silently dropped. It must now win (secrets/endpoint come from env).
    _clear_env(monkeypatch)
    home = tmp_path / "home"; home.mkdir()
    (home / "config.json").write_text(json.dumps(
        {"api_key": "file-key", "protocol": "openai",
         "api_base_url": "https://api.openai.com/v1", "model": "gpt-4"}))
    monkeypatch.setenv("CODING_AGENT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-xyz")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gw/anthropic")
    cfg = AgentConfig.resolve()
    assert cfg.api_key == "tok-xyz"
    assert cfg.protocol == "anthropic"
    assert cfg.extra_headers.get("Authorization") == "Bearer tok-xyz"


def test_anthropic_env_defaults_model_when_unset(tmp_path, monkeypatch):
    # Token set, no model env, no file model → must not stay the openai-family
    # default 'gpt-4' on an anthropic gateway; default to claude (matches TUI).
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nope"))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    cfg = AgentConfig.resolve()
    assert cfg.protocol == "anthropic"
    assert cfg.model == "claude-opus-4-8"


def test_anthropic_env_keeps_file_model_when_set(tmp_path, monkeypatch):
    # If config.json names a model and no model env var is set, the file model
    # must survive (the anthropic default only fills the gap).
    _clear_env(monkeypatch)
    home = tmp_path / "home"; home.mkdir()
    (home / "config.json").write_text(json.dumps({"model": "claude-sonnet-5"}))
    monkeypatch.setenv("CODING_AGENT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    cfg = AgentConfig.resolve()
    assert cfg.model == "claude-sonnet-5"
