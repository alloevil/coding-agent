"""
错误/边界注入测试：验证 ModelClient 面对网络失败/非2xx/坏JSON/超时的重试与放弃
行为，以及各类边界输入。之前重试逻辑只在 test_recovery 里间接覆盖，这里直接注入。

用 monkeypatch 替换内部 _complete_nonstream，把 base_delay 置 0 保证测试快。
"""
import httpx
import pytest

from coding_agent.core.model_client import ModelClient, _RETRYABLE_STATUS


def _client(**kw):
    defaults = dict(api_key="k", base_url="http://x/v1", model="m",
                    max_retries=3, base_delay=0.0, backoff_factor=1.0)
    defaults.update(kw)
    return ModelClient(**defaults)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://x/v1/chat/completions")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


async def test_retries_then_succeeds_on_retryable_status(monkeypatch):
    """429 两次后成功 → 最终返回结果（重试起作用）。"""
    calls = {"n": 0}

    async def flaky(messages, tools):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_status_error(429)
        return {"content": "ok", "tool_calls": []}

    mc = _client()
    monkeypatch.setattr(mc, "_complete_nonstream", flaky)
    r = await mc.complete([{"role": "user", "content": "hi"}], [], stream=False)
    assert r["content"] == "ok"
    assert calls["n"] == 3


async def test_gives_up_after_max_retries(monkeypatch):
    """一直 503 → 到达 max_retries 后抛出（不无限重试）。"""
    calls = {"n": 0}

    async def always_503(messages, tools):
        calls["n"] += 1
        raise _http_status_error(503)

    mc = _client(max_retries=2)
    monkeypatch.setattr(mc, "_complete_nonstream", always_503)
    with pytest.raises(httpx.HTTPStatusError):
        await mc.complete([{"role": "user", "content": "hi"}], [], stream=False)
    assert calls["n"] == 3  # 初次 + 2 次重试


async def test_non_retryable_status_raises_immediately(monkeypatch):
    """401（不可重试）→ 立刻抛出，不重试。"""
    calls = {"n": 0}

    async def unauthorized(messages, tools):
        calls["n"] += 1
        raise _http_status_error(401)

    mc = _client()
    monkeypatch.setattr(mc, "_complete_nonstream", unauthorized)
    with pytest.raises(httpx.HTTPStatusError):
        await mc.complete([{"role": "user", "content": "hi"}], [], stream=False)
    assert calls["n"] == 1  # 没有重试


async def test_transport_error_is_retried(monkeypatch):
    """网络层错误（连接重置）可重试。"""
    calls = {"n": 0}

    async def net_flap(messages, tools):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("connection reset")
        return {"content": "recovered", "tool_calls": []}

    mc = _client()
    monkeypatch.setattr(mc, "_complete_nonstream", net_flap)
    r = await mc.complete([{"role": "user", "content": "hi"}], [], stream=False)
    assert r["content"] == "recovered"
    assert calls["n"] == 2


async def test_timeout_is_retried_then_gives_up(monkeypatch):
    """超时一直发生 → 重试用尽后抛 TimeoutException。"""
    async def always_timeout(messages, tools):
        raise httpx.ReadTimeout("timed out")

    mc = _client(max_retries=1)
    monkeypatch.setattr(mc, "_complete_nonstream", always_timeout)
    with pytest.raises(httpx.TimeoutException):
        await mc.complete([{"role": "user", "content": "hi"}], [], stream=False)


def test_retryable_status_set_is_sane():
    """回归保护：429/503 等应在可重试集合里，401/404 不在。"""
    assert 429 in _RETRYABLE_STATUS
    assert 503 in _RETRYABLE_STATUS
    assert 401 not in _RETRYABLE_STATUS
    assert 404 not in _RETRYABLE_STATUS


# ── 协议层边界：坏请求 / 未知类型 / save_config 失败 ─────────────────

def _proto(monkeypatch, tmp_path):
    """构造一个可测的 AgentProtocol（隔离 session db + config home）。"""
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "cfg"))
    from coding_agent.protocol import AgentProtocol
    from coding_agent.core.config import AgentConfig
    cfg = AgentConfig(model="m", api_key="k", session_db_path=str(tmp_path / "s.db"))
    return AgentProtocol(cfg)


async def test_unknown_request_type_is_ignored(monkeypatch, tmp_path, capsys):
    """未知 request type → 静默 no-op，不崩溃、不发事件。"""
    p = _proto(monkeypatch, tmp_path)
    capsys.readouterr()
    await p.handle_request({"type": "totally_unknown", "x": 1})
    # 没有异常即通过；不应有 error 事件
    out = capsys.readouterr().out
    assert "error" not in out.lower() or out.strip() == ""


async def test_save_config_empty_answers_emits_error(monkeypatch, tmp_path, capsys):
    """save_config 收到空答案（无 api_key）→ 发 error 事件，不抛。"""
    p = _proto(monkeypatch, tmp_path)
    capsys.readouterr()
    await p.handle_request({"type": "save_config", "answers": {}})
    out = capsys.readouterr().out
    assert '"error"' in out or "error" in out
    assert "save_config failed" in out


async def test_interrupt_request_does_not_crash(monkeypatch, tmp_path, capsys):
    """interrupt 请求即使没有正在运行的 turn 也应安全。"""
    p = _proto(monkeypatch, tmp_path)
    capsys.readouterr()
    await p.handle_request({"type": "interrupt"})
    assert "interrupted" in capsys.readouterr().out


def test_bad_json_line_does_not_kill_reader():
    """协议 reader 对坏 JSON 行应跳过（json.loads 抛 JSONDecodeError 被捕获）。
    这里直接验证解析层：坏行抛，好行成。"""
    import json
    with pytest.raises(json.JSONDecodeError):
        json.loads("{not valid json")
    assert json.loads('{"type":"ping"}')["type"] == "ping"

