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
    # 后端协议：openai（/chat/completions）或 anthropic（/v1/messages）
    protocol: str = "openai"
    # extended-thinking 预算（仅 anthropic）：>0 时请求带 thinking，模型透出
    # 独立推理块（TUI 会显示"thinking"）。0=关闭。
    thinking_budget: int = 0
    
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
    # 写后自动格式化（prettier/gofmt/black/ruff/...，缺则跳过）。
    auto_format: bool = True
    # 细粒度权限规则：{"allow": [...], "deny": [...], "deny_read_paths": [...]}
    permissions: dict[str, Any] = field(default_factory=dict)
    # /cost 美元估价覆盖（每 1M token）：{"input": 3.0, "output": 15.0}。
    # 未设置且模型不在内置价表时，/cost 不显示美元。
    pricing: dict[str, Any] = field(default_factory=dict)
    # MCP servers：{"name": {"command": [...], "env": {...}, "cwd": "..."}}
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    # 生命周期 hook：{"pre_tool_use": [{"command": "..."}], "post_tool_use": [...], ...}
    hooks: dict[str, Any] = field(default_factory=dict)
    # 多 provider 配置：{"name": {"base_url":..., "api_key":..., "model":...,
    #   "extra_headers": {...}}}，供 /model 在会话中切换。
    providers: dict[str, Any] = field(default_factory=dict)
    
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
- Keep going until the task is genuinely complete. Don't hand back a half-finished result
  or stop at the first obstacle — work the problem step by step until it's solved.

# Verify before finishing — this is the #1 thing that separates good work from broken work
- After making changes, RUN them: tests (tdd_run_tests), the build, or the script itself via
  shell_exec. Re-read changed files to confirm the edit landed where you intended.
- A command or test that fails is NOT done. Tool results that start with `❌` or report a
  non-zero exit code / a failing test mean the work is broken — read the error, fix the
  cause, and re-run. Repeat until it actually passes. tdd_fix_loop can automate this cycle.
- NEVER claim a task is finished while there is an unresolved failure. If you genuinely
  cannot resolve it, say so explicitly and show the failing output — do not paper over it.
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
        
        # 优先 MODEL_API_KEY（OpenAI 兼容网关环境）
        if os.getenv("MODEL_API_KEY"):
            config.api_key = os.getenv("MODEL_API_KEY", "")
            config.api_base_url = os.getenv("MODEL_BASE_URL", "https://api.openai.com/v1")
            config.model = os.getenv("MODEL_PRIMARY", config.model)
        # Anthropic 原生协议网关（与 Rust TUI 的 env 读取保持一致）：
        # ANTHROPIC_AUTH_TOKEN → Bearer 头 + protocol=anthropic。
        elif os.getenv("ANTHROPIC_AUTH_TOKEN"):
            tok = os.getenv("ANTHROPIC_AUTH_TOKEN", "")
            config.api_key = tok
            config.protocol = "anthropic"
            if os.getenv("ANTHROPIC_BASE_URL"):
                config.api_base_url = os.getenv("ANTHROPIC_BASE_URL", "")
            # 网关多用 Authorization: Bearer（而非 x-api-key）；补上以免 401。
            headers = dict(config.extra_headers or {})
            headers.setdefault("Authorization", f"Bearer {tok}")
            config.extra_headers = headers
            model = os.getenv("CODING_AGENT_MODEL") or os.getenv("ANTHROPIC_MODEL")
            if model:
                # 剥掉网关不识别的方括号后缀（如 `[1m]`）。
                config.model = model.split("[", 1)[0]
        # OpenAI 兼容
        elif os.getenv("OPENAI_API_KEY"):
            config.api_key = os.getenv("OPENAI_API_KEY", "")
            if os.getenv("OPENAI_API_BASE"):
                config.api_base_url = os.getenv("OPENAI_API_BASE", "")
            if os.getenv("CODING_AGENT_MODEL"):
                config.model = os.getenv("CODING_AGENT_MODEL", "")
        # 通用 OpenAI 兼容网关 / 自建端点
        elif os.getenv("LLM_API_KEY"):
            config.api_key = os.getenv("LLM_API_KEY", "")
            if os.getenv("LLM_BASE_URL"):
                config.api_base_url = os.getenv("LLM_BASE_URL", "")
            if os.getenv("CODING_AGENT_MODEL"):
                config.model = os.getenv("CODING_AGENT_MODEL", "")

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
            # 仅当 env 真的指定了模型（≠ dataclass 默认）才覆盖文件里的模型，
            # 否则「设了 token 但没设 model」会把文件模型冲成默认。
            if env_cfg.model != cls().model:
                cfg.model = env_cfg.model
            # Anthropic env 路径还带 protocol + Authorization 头，一并生效，
            # 否则 config.json 已有 key 时这些会被静默丢弃。
            if env_cfg.protocol and env_cfg.protocol != cls().protocol:
                cfg.protocol = env_cfg.protocol
            if env_cfg.extra_headers:
                merged = dict(cfg.extra_headers or {})
                merged.update(env_cfg.extra_headers)
                cfg.extra_headers = merged
            # anthropic 网关但模型还是 openai 系默认 → 用 Anthropic 默认模型
            # （与 Rust TUI main.rs 一致），避免拿 `gpt-4` 打 anthropic 端点。
            if cfg.protocol == "anthropic" and cfg.model == cls().model:
                cfg.model = "claude-opus-4-8"
        return cfg

    def save(self, path: str) -> None:
        """保存配置到文件"""
        config_path = Path(path).expanduser()
        config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.__dict__, f, indent=2, ensure_ascii=False)

    def validate(self) -> list[str]:
        """
        启动前校验配置，返回人类可读的问题列表（含修复建议）。空列表 = 无问题。

        这些是最常见、且过去会导致「没反应 / 静默退出 / 503」的配错：
          - api_key 为空
          - model 带非法后缀（如 `[1m]` → 网关 model_not_found）
          - protocol 与 base_url 明显不匹配
          - anthropic 网关缺 Authorization / x-api-key 头
        """
        problems: list[str] = []

        if not str(self.api_key).strip():
            problems.append(
                "API key 为空。修复： coding-agent config set api_key <你的key>"
                "（或重跑 coding-agent --setup）"
            )

        # model 带方括号后缀（ANTHROPIC_DEFAULT_OPUS_MODEL 常见的 `[1m]`）→ 网关 404/503
        if self.model and ("[" in self.model or "]" in self.model):
            stripped = self.model.split("[", 1)[0]
            problems.append(
                f"model 含非法后缀： {self.model!r} —— 多数网关不识别方括号标记。"
                f"修复： coding-agent config set model {stripped}"
            )

        proto = (self.protocol or "openai").lower()
        base = (self.api_base_url or "").lower()

        if proto not in ("openai", "anthropic"):
            problems.append(
                f"protocol 无效： {self.protocol!r}（只支持 openai / anthropic）。"
                "修复： coding-agent config set protocol anthropic"
            )

        # protocol 与 base_url 明显对不上：官方 anthropic 域名却用 openai 协议，反之亦然。
        if base:
            if "api.anthropic.com" in base and proto != "anthropic":
                problems.append(
                    "base_url 指向 api.anthropic.com 但 protocol 不是 anthropic。"
                    "修复： coding-agent config set protocol anthropic"
                )
            if "api.openai.com" in base and proto != "openai":
                problems.append(
                    "base_url 指向 api.openai.com 但 protocol 不是 openai。"
                    "修复： coding-agent config set protocol openai"
                )

        # anthropic 协议需要鉴权头（x-api-key 或 Authorization: Bearer）。
        # ModelClient 的 _anthropic_headers 会用 api_key 兜底，所以这里只是「建议」级提醒，
        # 仅当 key 存在但网关可能要 Bearer（extra_headers 空）时提示，不算硬错误。
        return problems


def state_dir() -> Path:
    """
    可写状态目录（日志等），遵循 XDG：$XDG_STATE_HOME/coding-agent，
    否则 ~/.local/state/coding-agent。已 mkdir。

    统一所有日志落点，取代过去散落的 /tmp/tui.log 等。
    """
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    d = root / "coding-agent"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


# 允许 -c 命令行覆盖的字段 → 类型解析（拒绝随便写脏键）。
_OVERRIDE_KEYS: dict[str, Any] = {
    "model": str,
    "api_key": str,
    "api_base_url": str,
    "protocol": str,
    "auto_approve": lambda s: str(s).strip().lower() in ("1", "true", "yes", "y", "on"),
    "max_tokens": int,
    "max_turns": int,
    "max_total_tokens": int,
    "thinking_budget": int,
    "temperature": lambda s: None if str(s).strip().lower() in ("none", "null", "") else float(s),
    "stream": lambda s: str(s).strip().lower() in ("1", "true", "yes", "y", "on"),
    "auto_format": lambda s: str(s).strip().lower() in ("1", "true", "yes", "y", "on"),
}


def apply_cli_overrides(config: "AgentConfig", overrides: list[str]) -> "AgentConfig":
    """
    把 `-c key=value` 覆盖应用到已解析的 config（只对本次运行生效，不落盘）。

    对标 codex 的 `-c key=value`。值按类型解析（数字/布尔/None），未知键或坏值
    抛 ValueError（调用方给出清晰报错）。返回同一个 config（原地修改）。
    """
    for raw in overrides or []:
        if "=" not in raw:
            raise ValueError(f"bad override {raw!r} — expected KEY=VALUE")
        key, _, value = raw.partition("=")
        key = key.strip()
        if key not in _OVERRIDE_KEYS:
            raise ValueError(
                f"unknown config key: {key!r}. "
                f"overridable: {', '.join(sorted(_OVERRIDE_KEYS))}"
            )
        try:
            parsed = _OVERRIDE_KEYS[key](value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"invalid value for {key}: {value!r} ({e})") from e
        setattr(config, key, parsed)
    return config
