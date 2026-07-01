"""
测试 coding-agent doctor（core/doctor.py）：分级检查 + 修复建议 + JSON。
"""
import json

import pytest

from coding_agent.core.config import AgentConfig
from coding_agent.core import doctor as D


def _cfg(**kw):
    base = dict(api_key="sk-x", model="claude-opus-4-8", protocol="anthropic",
                api_base_url="http://gw/v1", extra_headers={"Authorization": "Bearer sk-x"})
    base.update(kw)
    return AgentConfig(**base)


def test_check_api_key_fail_on_empty():
    c = D.check_api_key(_cfg(api_key=""))
    assert c.level is D.Level.FAIL
    assert "config set api_key" in c.remediation


def test_check_model_flags_bracket_suffix():
    c = D.check_model(_cfg(model="claude-opus-4-8[1m]"))
    assert c.level is D.Level.FAIL
    assert "config set model claude-opus-4-8" in c.remediation


def test_check_protocol_mismatch():
    c = D.check_protocol_baseurl(_cfg(protocol="openai",
                                      api_base_url="https://api.anthropic.com"))
    assert c.level is D.Level.FAIL
    assert "anthropic" in c.remediation


def test_check_protocol_invalid():
    c = D.check_protocol_baseurl(_cfg(protocol="grpc"))
    assert c.level is D.Level.FAIL


def test_check_protocol_ok():
    c = D.check_protocol_baseurl(_cfg())
    assert c.level is D.Level.OK


def test_run_static_all_ok_for_good_config(tmp_path, monkeypatch):
    # 让 config.file 检查能通过：写一个真实文件
    from coding_agent.core.setup_wizard import global_config_path
    home = tmp_path / "cfg"
    monkeypatch.setenv("CODING_AGENT_HOME", str(home))
    p = global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"api_key": "sk-x"}), encoding="utf-8")
    rep = D.run_static(_cfg())
    # model/protocol/key 都好 → 无 FAIL
    assert rep.worst is not D.Level.FAIL, rep.render()


def test_report_worst_is_fail_when_any_fails():
    rep = D.Report(checks=[
        D.Check("a", D.Level.OK, "ok"),
        D.Check("b", D.Level.WARN, "warn"),
        D.Check("c", D.Level.FAIL, "fail"),
    ])
    assert rep.worst is D.Level.FAIL


def test_report_json_serializable():
    rep = D.Report(checks=[D.Check("x", D.Level.WARN, "s", detail="d", remediation="fix")])
    obj = json.loads(rep.to_json())
    assert obj["worst"] == "warn"
    assert obj["checks"][0]["id"] == "x"
    assert obj["checks"][0]["remediation"] == "fix"


def test_report_render_shows_remediation_for_problems():
    rep = D.Report(checks=[D.Check("x", D.Level.FAIL, "boom", remediation="do the thing")])
    text = rep.render()
    assert "boom" in text
    assert "do the thing" in text
    assert "1 failures" in text


@pytest.mark.asyncio
async def test_run_full_appends_probe(monkeypatch):
    # stub ModelClient.complete 让探测不打真实网络
    from coding_agent.core import model_client as MC

    async def fake_complete(self, *a, **k):
        return {"content": "ok"}

    monkeypatch.setattr(MC.ModelClient, "complete", fake_complete)
    rep = await D.run_full(_cfg())
    probe = [c for c in rep.checks if c.id == "endpoint.probe"]
    assert probe and probe[0].level is D.Level.OK


@pytest.mark.asyncio
async def test_probe_reports_failure(monkeypatch):
    from coding_agent.core import model_client as MC

    async def boom(self, *a, **k):
        raise RuntimeError("model_not_found")

    monkeypatch.setattr(MC.ModelClient, "complete", boom)
    c = await D.check_endpoint_reachable(_cfg())
    assert c.level is D.Level.FAIL
    assert "model_not_found" in c.detail
