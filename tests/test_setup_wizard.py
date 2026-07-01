"""
测试引导式配置向导（core/setup_wizard.py）。
"""
import json

from coding_agent.core import setup_wizard as W
from coding_agent.core.config import AgentConfig


def test_needs_setup():
    assert W.needs_setup(AgentConfig(api_key="")) is True
    assert W.needs_setup(AgentConfig(api_key="sk-x")) is False


def test_build_openai():
    d = W.build_config_dict({"provider": "openai", "api_key": "sk-x", "model": "gpt-4o"})
    assert d["api_base_url"] == "https://api.openai.com/v1"
    assert d["protocol"] == "openai"
    assert d["model"] == "gpt-4o"
    assert "extra_headers" not in d  # openai 不加 Bearer


def test_build_anthropic_adds_bearer_and_omits_temp():
    d = W.build_config_dict({"provider": "anthropic", "api_key": "tok", "model": "claude-opus-4-8"})
    assert d["protocol"] == "anthropic"
    assert d["extra_headers"]["Authorization"] == "Bearer tok"
    assert d["temperature"] is None  # 弃用 → 省略


def test_build_custom_uses_given_base_and_protocol():
    d = W.build_config_dict({"provider": "custom", "api_key": "k",
                             "base_url": "https://gw/v1", "protocol": "anthropic",
                             "model": "m"})
    assert d["api_base_url"] == "https://gw/v1"
    assert d["protocol"] == "anthropic"


def test_default_model_fallback():
    d = W.build_config_dict({"provider": "anthropic", "api_key": "k"})
    assert d["model"] == "claude-opus-4-8"  # preset 默认


def test_write_config_roundtrips_via_resolve(tmp_path, monkeypatch):
    home = tmp_path / "cfg"
    monkeypatch.setenv("CODING_AGENT_HOME", str(home))
    # 清掉可能干扰 resolve 的 env key
    for k in ("MODEL_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    W.write_config({"provider": "openai", "api_key": "sk-round", "model": "gpt-4o"},
                   home=str(home))
    cfg = AgentConfig.resolve()
    assert cfg.api_key == "sk-round"
    assert cfg.model == "gpt-4o"
    assert cfg.api_base_url == "https://api.openai.com/v1"


def test_write_config_merges_existing(tmp_path):
    home = tmp_path / "cfg"
    p = W.global_config_path(str(home))
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"max_turns": 42}), encoding="utf-8")
    W.write_config({"provider": "openai", "api_key": "k", "model": "gpt-4o"}, home=str(home))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["max_turns"] == 42       # 无关键保留
    assert data["api_key"] == "k"        # 新键写入


def test_cli_wizard_scripted(tmp_path, monkeypatch):
    home = tmp_path / "cfg"
    # 脚本化 stdin：provider=2(anthropic), key=tok, model=(默认), auto-approve=y
    answers_in = iter(["2", "tok", "", "y"])
    out = []
    res = W.run_cli_wizard(input_fn=lambda p: next(answers_in),
                           output_fn=out.append, home=str(home))
    assert res["provider"] == "anthropic"
    assert res["api_key"] == "tok"
    assert res["auto_approve"] is True
    # 落盘正确
    data = json.loads(W.global_config_path(str(home)).read_text(encoding="utf-8"))
    assert data["protocol"] == "anthropic"
    assert data["model"] == "claude-opus-4-8"
    assert data["extra_headers"]["Authorization"] == "Bearer tok"
    assert any("Saved to" in line for line in out)


def test_cli_wizard_custom_provider(tmp_path):
    answers_in = iter(["3", "https://gw/v1", "openai", "mykey", "mymodel", "n"])
    res = W.run_cli_wizard(input_fn=lambda p: next(answers_in),
                           output_fn=lambda s: None, home=str(tmp_path / "c"))
    assert res["base_url"] == "https://gw/v1"
    assert res["model"] == "mymodel"
    assert res["auto_approve"] is False
