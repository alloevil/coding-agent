"""
测试多 provider 配置 + /model 切换。
"""
from coding_agent.core.commands import dispatch, CommandContext
from coding_agent.core.config import AgentConfig


# ---- 命令 ----

def test_model_command_with_arg():
    r = dispatch("/model gpt-4o", CommandContext(tool_names=[]))
    assert r.kind == "action" and r.payload == "model:gpt-4o"


def test_model_command_provider_form():
    r = dispatch("/model openai:gpt-4o", CommandContext(tool_names=[]))
    assert r.payload == "model:openai:gpt-4o"


def test_model_command_no_arg():
    r = dispatch("/model", CommandContext(tool_names=[]))
    assert r.kind == "action" and r.payload == "model:"


def test_config_loads_providers(tmp_path):
    import json
    cfg_file = tmp_path / ".coding-agent.json"
    cfg_file.write_text(json.dumps({
        "providers": {"openai": {"base_url": "https://api.openai.com/v1",
                                 "api_key": "sk-x", "model": "gpt-4o"}}
    }), encoding="utf-8")
    cfg = AgentConfig.from_file(str(cfg_file))
    assert "openai" in cfg.providers
    assert cfg.providers["openai"]["model"] == "gpt-4o"


# ---- _switch_model 逻辑（用最小 stub，不起真 CodingAgent）----

class _FakeMC:
    def __init__(self):
        self.api_key = "old"
        self.base_url = "http://old"
        self.model = "old-model"
        self.extra_headers = {}


class _Stub:
    """只带 _switch_model 需要的字段。"""
    def __init__(self, providers):
        self.config = AgentConfig(model="old-model", api_key="k", providers=providers)
        self.model_client = _FakeMC()
        self.state = None
    # 借用真实方法
    from coding_agent.main import CodingAgent
    _switch_model = CodingAgent._switch_model


def test_switch_to_known_provider():
    s = _Stub({"openai": {"base_url": "https://api.openai.com/v1",
                          "api_key": "sk-new", "model": "gpt-4o",
                          "extra_headers": {"X-Foo": "1"}}})
    s._switch_model("openai")
    assert s.model_client.model == "gpt-4o"
    assert s.model_client.api_key == "sk-new"
    assert s.model_client.base_url == "https://api.openai.com/v1"
    assert s.model_client.extra_headers == {"X-Foo": "1"}
    assert s.config.model == "gpt-4o"


def test_switch_provider_with_model_override():
    s = _Stub({"openai": {"base_url": "https://api.openai.com/v1",
                          "api_key": "sk-new", "model": "gpt-4o"}})
    s._switch_model("openai:gpt-4o-mini")
    assert s.model_client.model == "gpt-4o-mini"  # spec 里的模型覆盖 provider 默认


def test_switch_bare_model_name():
    s = _Stub({})
    s._switch_model("claude-3-5-sonnet")
    assert s.model_client.model == "claude-3-5-sonnet"
    # 未配置 provider → 只换模型名，不动 url/key
    assert s.model_client.base_url == "http://old"


def test_switch_empty_shows_current(capsys):
    s = _Stub({"openai": {"model": "gpt-4o"}})
    s._switch_model("")
    out = capsys.readouterr().out
    assert "old-model" in out and "openai" in out
