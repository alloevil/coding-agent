"""
引导式配置向导 - 首次运行时带用户完成 provider / key / model 配置

配置的唯一写入真源（Python 侧）：
- needs_setup(config): 是否需要引导（api_key 为空）
- PROVIDERS: 常见 provider 预设（自动填 base_url / protocol / headers）
- write_config(answers): 只把有意义的键写入全局 config.json（resolve() 读的位置）
- run_cli_wizard(): 纯文本交互向导（Python CLI 后备；无 cargo / 直接用 CLI 时）

Rust 全屏 TUI 通过协议的 save_config 请求复用 write_config，界面原生但存储逻辑
不重复。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable


# provider 预设：base_url / protocol / 是否需要 Bearer header / 是否省略 temperature
PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI (api.openai.com)",
        "api_base_url": "https://api.openai.com/v1",
        "protocol": "openai",
        "default_model": "gpt-4o",
        "bearer_header": False,
        "omit_temperature": False,
        "key_help": "Get a key at https://platform.openai.com/api-keys",
    },
    "anthropic": {
        "label": "Anthropic / Claude (Messages API)",
        "api_base_url": "https://api.anthropic.com",
        "protocol": "anthropic",
        "default_model": "claude-opus-4-8",
        "bearer_header": True,     # 部分网关要求 Authorization: Bearer
        "omit_temperature": True,  # 较新 Claude 模型弃用 temperature
        "key_help": "Get a key at https://console.anthropic.com/settings/keys",
    },
    "custom": {
        "label": "Custom OpenAI-compatible gateway",
        "api_base_url": "",        # 向用户询问
        "protocol": "openai",
        "default_model": "",
        "bearer_header": False,
        "omit_temperature": False,
        "key_help": "Use the API key / token your gateway issued",
    },
}


def global_config_path(home: str | None = None) -> Path:
    """全局 config.json 路径（与 config.resolve() 读取位置一致）。"""
    base = home or os.environ.get("CODING_AGENT_HOME")
    root = Path(base) if base else Path.home() / ".config" / "coding-agent"
    return root / "config.json"


def needs_setup(config: Any) -> bool:
    """是否需要引导配置：没有 api_key 即需要。"""
    return not getattr(config, "api_key", "")


def build_config_dict(answers: dict[str, Any]) -> dict[str, Any]:
    """
    把向导答案组装成要写入 config.json 的字典（只含有意义的键）。

    answers 期望键：provider, api_key, model, base_url(custom 时),
    protocol(custom 可选), auto_approve(bool), temperature(float|None),
    tokenizer(bool，仅提示用，不落配置项)。
    """
    provider = answers.get("provider", "openai")
    preset = PROVIDERS.get(provider, PROVIDERS["openai"])

    base_url = answers.get("base_url") or preset["api_base_url"]
    protocol = answers.get("protocol") or preset["protocol"]
    model = answers.get("model") or preset["default_model"]
    key = answers.get("api_key", "")

    out: dict[str, Any] = {
        "api_key": key,
        "api_base_url": base_url,
        "model": model,
        "protocol": protocol,
    }
    # Bearer header（部分 Anthropic 网关需要）
    if preset.get("bearer_header") and key:
        out["extra_headers"] = {"Authorization": f"Bearer {key}"}
    # 较新 Claude 模型弃用 temperature → 写 None（config 会省略该字段）
    if preset.get("omit_temperature"):
        out["temperature"] = None
    elif answers.get("temperature") is not None:
        out["temperature"] = answers["temperature"]
    if "auto_approve" in answers:
        out["auto_approve"] = bool(answers["auto_approve"])
    return out


def write_config(answers: dict[str, Any], home: str | None = None) -> Path:
    """把向导答案写入全局 config.json（合并已有内容，不覆盖无关键）。返回路径。

    拒绝写入空 api_key —— 否则 needs_setup 永远为真、每次启动都进向导。
    """
    if not str(answers.get("api_key", "")).strip():
        raise ValueError("api_key is required and must not be empty")
    path = global_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing.update(build_config_dict(answers))
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ── CLI 文本向导（Python 后备） ──────────────────────────────────────

def run_cli_wizard(input_fn: Callable[[str], str] = input,
                   output_fn: Callable[[str], None] = print,
                   home: str | None = None) -> dict[str, Any]:
    """
    交互式文本向导。返回写入的答案；同时落盘到全局 config.json。

    input_fn / output_fn 可注入以便测试（脚本化 stdin）。
    """
    output_fn("")
    output_fn("👋 Welcome to coding-agent — let's set up your model provider.")
    output_fn("")

    # 1. provider
    keys = list(PROVIDERS.keys())
    output_fn("Choose a provider:")
    for i, k in enumerate(keys, 1):
        output_fn(f"  {i}. {PROVIDERS[k]['label']}")
    choice = input_fn("Provider [1]: ").strip() or "1"
    idx = int(choice) - 1 if choice.isdigit() and 1 <= int(choice) <= len(keys) else 0
    provider = keys[idx]
    preset = PROVIDERS[provider]

    answers: dict[str, Any] = {"provider": provider}

    # 2. custom：问 base_url + protocol
    if provider == "custom":
        answers["base_url"] = input_fn("Base URL (e.g. https://host/v1): ").strip()
        proto = input_fn("Protocol [openai/anthropic] (openai): ").strip() or "openai"
        answers["protocol"] = "anthropic" if proto.startswith("a") else "openai"

    # 3. API key —— 必填，空则重问（否则会存空 key，每次启动都进向导）
    if preset.get("key_help"):
        output_fn(f"  ({preset['key_help']})")
    key = input_fn("API key: ").strip()
    attempts = 0
    while not key and attempts < 5:
        output_fn("  ⚠️  API key is required.")
        key = input_fn("API key: ").strip()
        attempts += 1
    if not key:
        output_fn("  No API key provided — setup aborted.")
        raise KeyboardInterrupt("empty api key")
    answers["api_key"] = key

    # 4. model
    default_model = preset["default_model"] or "(required)"
    model = input_fn(f"Model [{default_model}]: ").strip()
    answers["model"] = model or preset["default_model"]

    # 5. 常用选项
    aa = input_fn("Auto-approve tool actions without asking? [y/N]: ").strip().lower()
    answers["auto_approve"] = aa in ("y", "yes")

    path = write_config(answers, home=home)
    output_fn("")
    output_fn(f"✅ Saved to {path}. You're all set!")
    output_fn("")
    return answers
