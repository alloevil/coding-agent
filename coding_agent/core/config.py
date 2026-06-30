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


def _read_json(path: Path) -> dict[str, Any]:
    """读取一个 JSON 配置文件；不存在或解析失败则返回空 dict。"""
    try:
        if path.is_file():
            import json as _json
            data = _json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


@dataclass
class AgentConfig:
    """Agent 配置"""
    
    # 模型配置
    model: str = "gpt-4"
    api_key: str = ""
    api_base_url: str = "https://api.openai.com/v1"
    max_tokens: int = 4096
    temperature: float | None = 0.7  # None 时省略该字段（GPT-5 等只接受默认温度）
    extra_headers: dict[str, str] = field(default_factory=dict)  # 网关自定义 header
    
    # Context 配置
    max_context_tokens: int = 200000
    auto_compact: bool = True
    
    # 行为配置
    auto_approve: bool = False
    max_turns: int = 100
    stream: bool = True  # 是否流式输出
    # 累计 token 预算（输入+输出+推理）；<=0 表示不限制。超出后停止本轮循环。
    max_total_tokens: int = 0
    # 单个工具执行超时（秒）；<=0 表示不限制。防止挂死调用冻结 agent。
    tool_timeout_seconds: float = 120.0
    # 细粒度权限规则：{"allow": [...], "deny": [...], "deny_read_paths": [...]}
    permissions: dict[str, Any] = field(default_factory=dict)
    # MCP servers：{"name": {"command": [...], "env": {...}, "cwd": "..."}}
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    # 生命周期 hook：{"pre_tool_use": [{"command": "..."}], "post_tool_use": [...], ...}
    hooks: dict[str, Any] = field(default_factory=dict)
    
    # 会话配置
    session_db_path: str = "/tmp/.coding-agent/sessions.db"
    
    # 系统提示词
    system_prompt: str = """You are a coding agent that helps users with software \
engineering tasks in their terminal. Use the tools available to you to do the work \
directly — read files, search, edit, run commands, run tests — rather than only \
describing what to do.

Tools you can use:
- Read and write files (file_read, file_write, file_edit, apply_patch)
- Execute shell commands (shell_exec)
- Search code (grep, file_search, list_files)
- Manage git (git_status, git_diff, git_commit, git_branch, git_log)
- Run the project's tests (tdd_run_tests)
- Track multi-step work (update_plan)
- Load a named skill's full instructions on demand (skill)
- Ask the user when genuinely blocked on a decision (ask_user)

# Following conventions
When you change code, first understand the surrounding file: its style, the libraries
and patterns it already uses, and match them. NEVER assume a library is available just
because it is well known — check that the codebase already depends on it (look at imports
in neighboring files, pyproject.toml/package.json/go.mod, etc.) before you use it. When
adding to an existing file, mimic its naming, typing, and structure so your change reads
like the code around it.

# Code style
- Do NOT add comments unless the change is genuinely non-obvious or the user asks. Match
  the comment density of the surrounding code.
- Never introduce code that logs or exposes secrets/keys. Never commit secrets.

# Output style
Be concise and direct — your output is shown in a terminal. Skip preamble and postamble:
don't open with "I'll now..." or close with a summary of what you changed unless asked.
When you reference code, use `file_path:line_number` so the user can jump to it. Answer
questions directly; for a simple question, a short answer is best.

# Doing tasks
- For any task with more than ~2 steps, call update_plan first to lay out the steps, then
  keep it current: exactly one step in_progress at a time, mark steps completed as you go.
- When you need several independent pieces of information, issue the read-only tool calls
  together so they run in parallel.
- Before declaring a task done, VERIFY: run tdd_run_tests (or the project's build/lint/test
  command via shell_exec) and re-read changed files. Do not claim success on unverified
  changes; if a check fails, say so with the output.
- NEVER commit changes unless the user explicitly asks you to.
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

    @classmethod
    def resolve(cls) -> "AgentConfig":
        """
        分层解析配置（后者覆盖前者）：
          1. 默认值
          2. 全局配置 ~/.config/coding-agent/config.json（或 $CODING_AGENT_HOME）
          3. 项目配置 ./.coding-agent.json
          4. 环境变量（密钥以 env 为准，避免把 key 写进文件）

        非密钥字段由配置文件覆盖默认；密钥/端点最终由 from_env 决定（若设置了）。
        """
        import os as _os

        # 1. 默认
        data: dict[str, Any] = {}

        # 2. 全局配置文件
        home = _os.environ.get("CODING_AGENT_HOME")
        global_path = (Path(home) if home else Path.home() / ".config" / "coding-agent") / "config.json"
        data.update(_read_json(global_path))

        # 3. 项目配置文件
        data.update(_read_json(Path.cwd() / ".coding-agent.json"))

        # 只保留合法字段
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

        # 4. 环境变量覆盖密钥/端点/模型（复用 from_env 的解析）
        env_cfg = cls.from_env()
        if env_cfg.api_key:
            cfg.api_key = env_cfg.api_key
            cfg.api_base_url = env_cfg.api_base_url
            cfg.model = env_cfg.model
        return cfg

    def save(self, path: str) -> None:
        """保存配置到文件"""
        config_path = Path(path).expanduser()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.__dict__, f, indent=2, ensure_ascii=False)
