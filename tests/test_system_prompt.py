"""
锁定默认系统提示词里的关键生产约定。

这些子句直接影响任务正确性（不要臆造依赖、未经允许不要提交、完成前先验证），
属于"行为护栏"而非措辞偏好，因此用测试固定下来，防止后续编辑悄悄丢掉。
"""
from coding_agent.core.config import AgentConfig


def test_system_prompt_has_production_conventions():
    p = AgentConfig().system_prompt.lower()
    # 不要臆造库 / 先确认依赖
    assert "never assume" in p and "library" in p
    # 未经明确要求不要提交
    assert "never commit" in p and "explicitly" in p
    # 完成前先验证
    assert "verify" in p and "tdd_run_tests" in p
    # 跟随既有代码风格
    assert "conventions" in p
    # file:line 引用约定
    assert "file_path:line_number" in p
    # 不要随意加注释
    assert "comment" in p


def test_system_prompt_emphasizes_verify_and_persistence():
    """强化的 verify/persistence 段（对标 opencode beast.txt 的核心要求）。"""
    p = AgentConfig().system_prompt.lower()
    # 坚持到完成
    assert "keep going" in p
    # 失败必须修、不许带未解决失败收尾
    assert "fail" in p and "re-run" in p
    assert "never claim" in p or "unresolved failure" in p
    # 与框架失败信号呼应（❌ / non-zero exit）
    assert "non-zero exit" in p


def test_system_prompt_lists_core_tools():
    p = AgentConfig().system_prompt
    for tool in ("file_edit", "shell_exec", "grep", "update_plan",
                 "git_branch", "ask_user"):
        assert tool in p
