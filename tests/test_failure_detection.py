"""
测试结构化失败检测：命令失败（exit code/超时/拦截）应让 is_error=True，
而不只是 "Error" 开头的字符串；且确定性失败不进重试。
"""
from coding_agent.core.agent import _looks_like_failure


def test_error_prefix_is_failure():
    assert _looks_like_failure("Error: something broke") is True


def test_nonzero_exit_is_failure():
    out = "❌ Command failed (exit code 1)\n\nstderr:\ntraceback...\n\nexit code: 1"
    assert _looks_like_failure(out) is True


def test_timeout_is_failure():
    assert _looks_like_failure("⏱ TIMEOUT: Command timed out after 30 seconds") is True


def test_blocked_is_failure():
    assert _looks_like_failure("🚫 BLOCKED: write to protected path") is True


def test_success_output_not_failure():
    assert _looks_like_failure("stdout:\nHello world\n") is False
    assert _looks_like_failure("Command executed successfully (no output)") is False


def test_plain_text_not_failure():
    # 普通包含 "error" 词但不是失败标记的文本（如代码片段）
    assert _looks_like_failure("def handle_error(): pass") is False


def test_non_string_safe():
    assert _looks_like_failure(None) is False
    assert _looks_like_failure(123) is False
