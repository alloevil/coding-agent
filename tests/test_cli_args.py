"""
测试 CLI 参数解析（--resume / --list-sessions）
"""
import pytest

from coding_agent.main import _parse_args


def test_no_args():
    o = _parse_args([])
    assert o["resume"] is None
    assert o["list_sessions"] is False


def test_resume_with_id():
    o = _parse_args(["--resume", "abc123"])
    assert o["resume"] == "abc123"


def test_resume_without_id_picks():
    o = _parse_args(["--resume"])
    assert o["resume"] == "__PICK__"


def test_list_sessions():
    o = _parse_args(["--list-sessions"])
    assert o["list_sessions"] is True


@pytest.mark.asyncio
async def test_main_list_sessions_no_key(tmp_path, monkeypatch, capsys):
    # --list-sessions 不需要 API key，也不应 sys.exit
    for k in ("MODEL_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path / "nope"))
    monkeypatch.chdir(tmp_path)
    # 指向一个空的临时 session db
    monkeypatch.setattr("coding_agent.core.config.AgentConfig.session_db_path",
                        str(tmp_path / "s.db"), raising=False)
    from coding_agent.main import main
    await main(["--list-sessions"])
    out = capsys.readouterr().out
    assert "session" in out.lower()
