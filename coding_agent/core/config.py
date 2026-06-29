"""
配置管理

参考 Claude Code 的透明文件配置设计
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentConfig:
    """Agent 配置"""
    
    # 模型配置
    model: str = "gpt-4"
    api_key: str = ""
    api_base_url: str = "https://api.openai.com/v1"
    max_tokens: int = 4096
    temperature: float = 0.7
    
    # Context 配置
    max_context_tokens: int = 200000
    auto_compact: bool = True
    
    # 行为配置
    auto_approve: bool = False
    max_turns: int = 100
    stream: bool = True  # 是否流式输出
    
    # 会话配置
    session_db_path: str = "/tmp/.coding-agent/sessions.db"
    
    # 系统提示词
    system_prompt: str = """You are a helpful AI coding assistant. You can:
- Read and write files (file_read, file_write, file_edit, apply_patch)
- Execute shell commands (shell_exec)
- Search code (grep, file_search, list_files)
- Manage git repositories (git_status, git_diff, git_commit)
- Run the project's tests (tdd_run_tests)
- Track multi-step work (update_plan)

Always think step by step before taking action. When editing files, make minimal, focused changes.

For any task with more than ~2 steps, call update_plan first to lay out the steps,
then keep it current: mark exactly one step in_progress, and mark steps completed
as you finish them.

Before declaring a task done, VERIFY your work: run tdd_run_tests (or the relevant
build/test command via shell_exec) and re-read changed files. Do not claim success
on unverified changes.
"""
    
    @classmethod
    def from_file(cls, path: str) -> AgentConfig:
        """从文件加载配置"""
        config_path = Path(path).expanduser()
        if not config_path.exists():
            return cls()
        
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    @classmethod
    def from_env(cls) -> AgentConfig:
        """从环境变量加载配置"""
        config = cls()
        
        # 优先 MODEL_API_KEY（OpenClaw 环境）
        if os.getenv("MODEL_API_KEY"):
            config.api_key = os.getenv("MODEL_API_KEY", "")
            config.api_base_url = os.getenv("MODEL_BASE_URL", "http://model.mify.ai.srv/v1")
            # MODEL_PRIMARY 格式如 "custom-model-mify-ai-srv/xiaomi/mimo-v2.5-pro-mit"
            primary = os.getenv("MODEL_PRIMARY", "xiaomi/mimo-v2.5-pro")
            # 提取 xiaomi/xxx 部分
            if "/xiaomi/" in primary:
                config.model = "xiaomi/" + primary.split("/xiaomi/")[-1]
            elif primary.startswith("xiaomi/"):
                config.model = primary
            else:
                config.model = primary
        # OpenAI 兼容
        elif os.getenv("OPENAI_API_KEY"):
            config.api_key = os.getenv("OPENAI_API_KEY", "")
            if os.getenv("OPENAI_API_BASE"):
                config.api_base_url = os.getenv("OPENAI_API_BASE", "")
            if os.getenv("CODING_AGENT_MODEL"):
                config.model = os.getenv("CODING_AGENT_MODEL", "")
        # 小米 mify
        elif os.getenv("LLM_API_KEY"):
            config.api_key = os.getenv("LLM_API_KEY", "")
            if os.getenv("LLM_BASE_URL"):
                config.api_base_url = os.getenv("LLM_BASE_URL", "")
        
        return config
    
    def save(self, path: str) -> None:
        """保存配置到文件"""
        config_path = Path(path).expanduser()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.__dict__, f, indent=2, ensure_ascii=False)
