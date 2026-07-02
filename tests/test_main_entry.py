"""
入口编排测试：main.py 的 CLI 命令分发、config/doctor 子命令、setup 流程、
参数解析。这些是过去 bug 最集中处（空 key 循环、needs_setup、粘贴），
但覆盖率最低（main.py 曾 17%）。用 monkeypatch/capsys，不打真实网络/交互。
"""
import json

import pytest

from coding_agent import main as M
from coding_agent.core.config import AgentConfig


# ── 参数解析 ─────────────────────────────────────────────────────────

def test_parse_args_defaults():
    opts = M._parse_args([])
    assert opts["resume"] is None
    assert opts["tui"] is False
    assert opts["setup"] is False
    assert opts["list_sessions"] is False


def test_parse_args_flags():
    opts = M._parse_args(["--tui", "--setup"])
    assert opts["tui"] is True
    assert opts["setup"] is True


def test_parse_args_resume_pick_vs_id():
    assert M._parse_args(["--resume"])["resume"] == "__PICK__"
    assert M._parse_args(["--resume", "abc123"])["resume"] == "abc123"


# ── config 子命令 ────────────────────────────────────────────────────

@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "cfg"
    monkeypatch.setenv("CODING_AGENT_HOME", str(home))
    for k in ("MODEL_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    return home


def test_config_set_and_get(isolated_home, capsys):
    M._run_config_cmd(["set", "api_key", "sk-abc"])
    M._run_config_cmd(["set", "model", "claude-opus-4-8"])
    capsys.readouterr()  # 清空
    M._run_config_cmd(["get", "model"])
    out = capsys.readouterr().out
    assert "claude-opus-4-8" in out


def test_config_show_redacts(isolated_home, capsys):
    M._run_config_cmd(["set", "api_key", "sk-1234567890"])
    capsys.readouterr()
    M._run_config_cmd(["show"])
    out = capsys.readouterr().out
    assert "sk-1234567890" not in out       # 原文不出现
    assert "sk-1" in out and "7890" in out   # 打码保留头尾


def test_config_path(isolated_home, capsys):
    M._run_config_cmd(["path"])
    out = capsys.readouterr().out
    assert "config.json" in out


def test_config_set_unknown_key_exits_nonzero(isolated_home, capsys):
    with pytest.raises(SystemExit) as e:
        M._run_config_cmd(["set", "bogus", "x"])
    assert e.value.code == 1
    assert "unknown config key" in capsys.readouterr().out


def test_config_help_when_empty(isolated_home, capsys):
    M._run_config_cmd([])
    assert "Usage" in capsys.readouterr().out


def test_config_set_missing_value_exits(isolated_home, capsys):
    with pytest.raises(SystemExit) as e:
        M._run_config_cmd(["set", "model"])
    assert e.value.code == 2


# ── doctor 子命令 ────────────────────────────────────────────────────

async def test_doctor_json_on_bad_config_exits_1(isolated_home, capsys, monkeypatch):
    # 坏配置：空 key（FAIL）→ 退出码 1
    with pytest.raises(SystemExit) as e:
        await M._run_doctor_cmd(["--json"])
    assert e.value.code == 1
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["worst"] == "fail"
    assert any(c["id"] == "auth.key" for c in obj["checks"])


async def test_doctor_human_readable(isolated_home, capsys):
    M._run_config_cmd(["set", "api_key", "sk-x"])
    M._run_config_cmd(["set", "model", "claude-opus-4-8"])
    M._run_config_cmd(["set", "protocol", "anthropic"])
    M._run_config_cmd(["set", "api_base_url", "http://gw/v1"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        await M._run_doctor_cmd([])
    assert e.value.code == 0  # 好配置 → 无 FAIL → 0
    out = capsys.readouterr().out
    assert "doctor" in out and "ok" in out.lower()


# ── 启动校验 ─────────────────────────────────────────────────────────

def test_warn_if_invalid_prints_problems(capsys):
    bad = AgentConfig(api_key="", model="m[1m]", protocol="openai",
                      api_base_url="https://api.anthropic.com")
    M._warn_if_invalid(bad)
    out = capsys.readouterr().out
    assert "配置有问题" in out or "API key" in out


def test_warn_if_invalid_silent_when_ok(capsys):
    good = AgentConfig(api_key="sk-x", model="claude-opus-4-8",
                       protocol="anthropic", api_base_url="http://gw/v1")
    M._warn_if_invalid(good)
    assert capsys.readouterr().out == ""


# ── main() 分发：config/doctor 早退不进 agent ────────────────────────

async def test_main_config_subcommand_returns_early(isolated_home, capsys, monkeypatch):
    # 若 main 误进 agent，会因无 key 触发向导/报错；这里应在 config 处就 return。
    called = {"agent": False}

    def _boom(*a, **k):
        called["agent"] = True
        raise AssertionError("should not construct agent for `config` subcommand")

    monkeypatch.setattr(M, "CodingAgent", _boom)
    await M.main(["config", "path"])
    assert called["agent"] is False
    assert "config.json" in capsys.readouterr().out


async def test_main_doctor_subcommand_returns_early(isolated_home, capsys, monkeypatch):
    monkeypatch.setattr(M, "CodingAgent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no agent")))
    with pytest.raises(SystemExit):
        await M.main(["doctor", "--json"])
    # 输出是 doctor 的 JSON，而不是 agent 会话
    assert "checks" in capsys.readouterr().out


async def test_main_update_subcommand_returns_early(isolated_home, monkeypatch):
    """`update` 子命令应调 run_update 并退出，不构建 agent。"""
    called = {"update": False}

    def fake_update(*a, **k):
        called["update"] = True
        return 0

    monkeypatch.setattr("coding_agent.core.updater.run_update", fake_update)
    monkeypatch.setattr(M, "CodingAgent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no agent")))
    with pytest.raises(SystemExit) as e:
        await M.main(["update"])
    assert e.value.code == 0
    assert called["update"] is True


def test_parse_args_collects_c_overrides():
    opts = M._parse_args(["-c", "model=x", "-c", "protocol=anthropic"])
    assert opts["overrides"] == ["model=x", "protocol=anthropic"]


def test_parse_args_print_mode():
    opts = M._parse_args(["-p", "explain this repo"])
    assert opts["print"] == "explain this repo"
    assert M._parse_args([])["print"] is None


async def test_run_print_outputs_final_reply(isolated_home, capsys, monkeypatch, tmp_path):
    """run_print：mock 模型一轮文本回复 → stdout 只有最终答案，退出码 0。"""
    from coding_agent.core.config import AgentConfig

    cfg = AgentConfig(api_key="k", model="mock", auto_approve=True,
                      session_db_path=str(tmp_path / "s.db"))
    agent = M.CodingAgent(cfg)

    async def fake_model(context, tools):
        return {"content": "the final answer", "tool_calls": []}

    agent.agent_loop.set_model_call_fn(fake_model)
    code = await agent.run_print("do the thing")
    assert code == 0
    out = capsys.readouterr().out
    assert "the final answer" in out
    # 无横幅噪声
    assert "Coding Agent started" not in out
    # headless 必须自动放行，否则会阻塞在交互式权限确认
    assert agent.agent_loop.permission_policy.auto_approve is True
