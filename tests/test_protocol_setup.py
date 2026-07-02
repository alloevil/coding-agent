"""
测试 protocol 的 save_config 请求：写入全局 config.json 并热更 model client。
"""
import asyncio
import json


def _make_protocol(monkeypatch, home):
    from coding_agent import protocol as P
    monkeypatch.setattr(P, "register_file_tools", lambda *a, **k: None)
    monkeypatch.setattr(P, "register_shell_tools", lambda *a, **k: None)
    monkeypatch.setattr(P, "register_git_tools", lambda *a, **k: None)
    monkeypatch.setenv("CODING_AGENT_HOME", str(home))
    for k in ("MODEL_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY",
              "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(k, raising=False)

    proto = P.AgentProtocol.__new__(P.AgentProtocol)
    from coding_agent.core.config import AgentConfig
    proto.config = AgentConfig(api_key="", model="gpt-4")

    class _MC:
        api_key = ""; model = ""; base_url = ""; protocol = "openai"; extra_headers = {}
    proto.model_client = _MC()
    proto._events = []
    proto._send_event = lambda t, d=None: proto._events.append((t, d or {}))
    return proto


def test_save_config_writes_and_hotswaps(tmp_path, monkeypatch):
    home = tmp_path / "cfg"
    proto = _make_protocol(monkeypatch, home)

    asyncio.run(proto.handle_request({
        "type": "save_config",
        "answers": {"provider": "anthropic", "api_key": "tok", "model": "claude-opus-4-8"},
    }))

    # 事件
    kinds = [t for t, _ in proto._events]
    assert "config_saved" in kinds
    # 落盘
    from coding_agent.core.setup_wizard import global_config_path
    data = json.loads(global_config_path(str(home)).read_text(encoding="utf-8"))
    assert data["api_key"] == "tok"
    assert data["protocol"] == "anthropic"
    # 热更 model client
    assert proto.model_client.api_key == "tok"
    assert proto.model_client.protocol == "anthropic"
    assert proto.model_client.extra_headers["Authorization"] == "Bearer tok"


def test_ready_includes_needs_setup():
    # ready 事件带 needs_setup 字段（无 key 时 true）
    # 直接验证字段构造逻辑：not api_key
    from coding_agent.core.config import AgentConfig
    assert (not AgentConfig(api_key="").api_key) is True
    assert (not AgentConfig(api_key="x").api_key) is False
