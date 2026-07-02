"""
测试配置成熟度改进：
- AgentConfig.validate() 启动前校验（明确报错，不静默）
- state_dir() 统一日志目录
- setup_wizard.set_config_value / read_config / redact（改单项、打码）
"""
import json

import pytest

from coding_agent.core.config import AgentConfig, state_dir
from coding_agent.core import setup_wizard as W


# ── validate() ──────────────────────────────────────────────────────

def test_validate_ok_for_good_config():
    c = AgentConfig(api_key="sk-x", model="claude-opus-4-8",
                    protocol="anthropic", api_base_url="http://gw/v1")
    assert c.validate() == []


def test_validate_flags_empty_key():
    c = AgentConfig(api_key="", model="gpt-4o", protocol="openai")
    probs = c.validate()
    assert any("API key" in p for p in probs)


def test_validate_flags_bracket_suffix_in_model():
    c = AgentConfig(api_key="k", model="claude-opus-4-8[1m]", protocol="anthropic",
                    api_base_url="http://gw")
    probs = c.validate()
    assert any("[1m]" in p or "非法后缀" in p for p in probs)
    # 建议里给出去掉后缀的正确名
    assert any("claude-opus-4-8" in p for p in probs)


def test_validate_flags_protocol_baseurl_mismatch():
    c = AgentConfig(api_key="k", model="claude", protocol="openai",
                    api_base_url="https://api.anthropic.com")
    probs = c.validate()
    assert any("anthropic" in p for p in probs)


def test_validate_flags_invalid_protocol():
    c = AgentConfig(api_key="k", model="m", protocol="grpc", api_base_url="http://x")
    assert any("protocol" in p for p in c.validate())


# ── state_dir() ─────────────────────────────────────────────────────

def test_state_dir_uses_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = state_dir()
    assert d == tmp_path / "coding-agent"
    assert d.is_dir()


# ── set_config_value / read_config / redact ─────────────────────────

def test_set_config_value_single_key(tmp_path):
    home = tmp_path / "cfg"
    W.set_config_value("api_key", "sk-abc", home=str(home))
    W.set_config_value("model", "claude-opus-4-8", home=str(home))
    data = W.read_config(home=str(home))
    assert data["api_key"] == "sk-abc"
    assert data["model"] == "claude-opus-4-8"


def test_set_protocol_anthropic_adds_bearer(tmp_path):
    home = tmp_path / "cfg"
    W.set_config_value("api_key", "tok", home=str(home))
    W.set_config_value("protocol", "anthropic", home=str(home))
    data = W.read_config(home=str(home))
    assert data["extra_headers"]["Authorization"] == "Bearer tok"


def test_set_config_rejects_unknown_key(tmp_path):
    with pytest.raises(ValueError, match="unknown config key"):
        W.set_config_value("bogus", "x", home=str(tmp_path / "c"))


def test_set_config_rejects_empty_api_key(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        W.set_config_value("api_key", "   ", home=str(tmp_path / "c"))


def test_set_config_parses_bool_and_temperature(tmp_path):
    home = tmp_path / "cfg"
    W.set_config_value("api_key", "k", home=str(home))
    W.set_config_value("auto_approve", "yes", home=str(home))
    W.set_config_value("temperature", "none", home=str(home))
    data = W.read_config(home=str(home))
    assert data["auto_approve"] is True
    assert data["temperature"] is None


def test_redact_masks_key_and_bearer():
    out = W.redact({
        "api_key": "sk-1234567890",
        "extra_headers": {"Authorization": "Bearer sk-1234567890"},
        "model": "m",
    })
    assert out["api_key"] == "sk-1…7890"
    assert out["extra_headers"]["Authorization"].startswith("Bear")
    assert out["model"] == "m"  # 非敏感字段原样


def test_set_then_resolve_roundtrip(tmp_path, monkeypatch):
    home = tmp_path / "cfg"
    monkeypatch.setenv("CODING_AGENT_HOME", str(home))
    for k in ("MODEL_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY",
              "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(k, raising=False)
    W.set_config_value("api_key", "sk-round", home=str(home))
    W.set_config_value("model", "claude-opus-4-8", home=str(home))
    W.set_config_value("protocol", "anthropic", home=str(home))
    W.set_config_value("api_base_url", "http://gw/v1", home=str(home))
    cfg = AgentConfig.resolve()
    assert cfg.api_key == "sk-round"
    assert cfg.model == "claude-opus-4-8"
    assert cfg.protocol == "anthropic"
    assert cfg.validate() == []  # 组合起来是有效配置
