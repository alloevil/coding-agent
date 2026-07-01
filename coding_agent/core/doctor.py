"""
coding-agent doctor —— 本地配置 / 端点健康诊断。

参考 Codex `codex doctor` 的结构化设计：每项检查返回一行可序列化结果
（level + summary + 可选 detail + remediation），同一份数据既能渲染人类可读
报告，也能 `--json` 输出。诊断是「只读」的：只检查，不改用户状态。

用法（见 main.py 的 doctor 子命令）：
    coding-agent doctor            人类可读报告
    coding-agent doctor --json     机器可读
    coding-agent doctor --probe    额外做一次真实端点探测（打一发最小请求）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Level(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


# 排序 / 汇总用的严重度权重
_RANK = {Level.OK: 0, Level.WARN: 1, Level.FAIL: 2}

_ICON = {Level.OK: "✅", Level.WARN: "⚠️ ", Level.FAIL: "❌"}


@dataclass
class Check:
    """一项检查结果。id 机器可读、稳定；summary 人类可读；remediation 给修复建议。"""
    id: str
    level: Level
    summary: str
    detail: str = ""
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "level": self.level.value,
            "summary": self.summary,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def worst(self) -> Level:
        return max((c.level for c in self.checks), key=lambda l: _RANK[l], default=Level.OK)

    def to_json(self) -> str:
        return json.dumps(
            {"worst": self.worst.value, "checks": [c.to_dict() for c in self.checks]},
            indent=2, ensure_ascii=False,
        )

    def render(self) -> str:
        lines = ["", "🩺 coding-agent doctor", ""]
        for c in self.checks:
            lines.append(f"  {_ICON[c.level]} {c.summary}")
            if c.detail:
                lines.append(f"       {c.detail}")
            if c.level is not Level.OK and c.remediation:
                lines.append(f"       → {c.remediation}")
        lines.append("")
        tally = {
            "ok": sum(c.level is Level.OK for c in self.checks),
            "warn": sum(c.level is Level.WARN for c in self.checks),
            "fail": sum(c.level is Level.FAIL for c in self.checks),
        }
        lines.append(f"  {tally['ok']} ok · {tally['warn']} warnings · {tally['fail']} failures")
        lines.append("")
        return "\n".join(lines)


# ── 各项检查（纯函数，便于测试） ────────────────────────────────────

def check_config_readable(config: Any) -> Check:
    """全局 config.json 是否存在且可解析。"""
    from .setup_wizard import global_config_path
    p = global_config_path()
    if not p.is_file():
        return Check(
            "config.file", Level.WARN,
            "No config file yet",
            detail=f"expected at {p}",
            remediation="Run `coding-agent --setup` to create it",
        )
    try:
        json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return Check(
            "config.file", Level.FAIL,
            "config.json is present but unreadable / invalid JSON",
            detail=f"{p}: {e}",
            remediation="Fix the JSON, or re-run `coding-agent --setup` to rewrite it",
        )
    return Check("config.file", Level.OK, f"Config file OK ({p})")


def check_api_key(config: Any) -> Check:
    if str(getattr(config, "api_key", "")).strip():
        return Check("auth.key", Level.OK, "API key is set")
    return Check(
        "auth.key", Level.FAIL, "API key is empty",
        remediation="coding-agent config set api_key <your-key>  (or `coding-agent --setup`)",
    )


def check_model(config: Any) -> Check:
    model = getattr(config, "model", "") or ""
    if "[" in model or "]" in model:
        stripped = model.split("[", 1)[0]
        return Check(
            "model.name", Level.FAIL,
            f"Model name has an illegal suffix: {model!r}",
            detail="Most gateways reject bracketed markers like `[1m]` with model_not_found.",
            remediation=f"coding-agent config set model {stripped}",
        )
    if not model:
        return Check("model.name", Level.WARN, "No model set",
                     remediation="coding-agent config set model <model>")
    return Check("model.name", Level.OK, f"Model: {model}")


def check_protocol_baseurl(config: Any) -> Check:
    proto = (getattr(config, "protocol", "openai") or "openai").lower()
    base = (getattr(config, "api_base_url", "") or "").lower()
    if proto not in ("openai", "anthropic"):
        return Check(
            "protocol.value", Level.FAIL, f"Invalid protocol: {proto!r}",
            detail="Only 'openai' or 'anthropic' are supported.",
            remediation="coding-agent config set protocol anthropic",
        )
    if "api.anthropic.com" in base and proto != "anthropic":
        return Check("protocol.match", Level.FAIL,
                     "base_url points at api.anthropic.com but protocol is not anthropic",
                     remediation="coding-agent config set protocol anthropic")
    if "api.openai.com" in base and proto != "openai":
        return Check("protocol.match", Level.FAIL,
                     "base_url points at api.openai.com but protocol is not openai",
                     remediation="coding-agent config set protocol openai")
    return Check("protocol.match", Level.OK, f"Protocol/base_url consistent ({proto})")


def check_anthropic_auth_header(config: Any) -> Check:
    """anthropic 协议下，多数网关需要 Authorization/x-api-key 头。ModelClient 会兜底，
    但显式提示更清晰。"""
    proto = (getattr(config, "protocol", "openai") or "openai").lower()
    if proto != "anthropic":
        return Check("auth.header", Level.OK, "Auth header check not applicable (openai)")
    headers = getattr(config, "extra_headers", {}) or {}
    has = any(k.lower() in ("authorization", "x-api-key") for k in headers)
    if has or str(getattr(config, "api_key", "")).strip():
        return Check("auth.header", Level.OK, "Anthropic auth available")
    return Check("auth.header", Level.WARN,
                 "Anthropic protocol but no auth header and no api_key",
                 remediation="coding-agent config set api_key <token>")


def check_log_dir_writable() -> Check:
    from .config import state_dir
    d = state_dir()
    try:
        probe = d / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return Check("state.log_dir", Level.WARN,
                     f"Log/state dir not writable: {d}", detail=str(e))
    return Check("state.log_dir", Level.OK, f"State dir writable ({d})")


async def check_endpoint_reachable(config: Any) -> Check:
    """真实端点探测（--probe）：打一发最小请求，确认端点+key+model 组合能通。"""
    from .model_client import ModelClient
    try:
        mc = ModelClient(
            api_key=getattr(config, "api_key", ""),
            base_url=getattr(config, "api_base_url", ""),
            model=getattr(config, "model", ""),
            protocol=getattr(config, "protocol", "openai"),
            temperature=None,
            extra_headers=getattr(config, "extra_headers", {}) or {},
        )
        r = await mc.complete([{"role": "user", "content": "reply with: ok"}], [])
        content = (r.get("content") or "").strip()
        return Check("endpoint.probe", Level.OK,
                     "Endpoint reachable — model replied",
                     detail=f"reply: {content[:60]!r}")
    except Exception as e:  # noqa: BLE001 — 探测要吞掉一切，只报告
        msg = str(e)
        return Check(
            "endpoint.probe", Level.FAIL,
            "Endpoint probe failed",
            detail=msg[:200],
            remediation="Check base_url / api_key / model. "
                        "If model_not_found: strip any `[..]` suffix.",
        )


def run_static(config: Any) -> Report:
    """不打网络的静态诊断（doctor 默认）。"""
    return Report(checks=[
        check_config_readable(config),
        check_api_key(config),
        check_model(config),
        check_protocol_baseurl(config),
        check_anthropic_auth_header(config),
        check_log_dir_writable(),
    ])


async def run_full(config: Any) -> Report:
    """静态 + 端点探测（doctor --probe）。"""
    rep = run_static(config)
    rep.checks.append(await check_endpoint_reachable(config))
    return rep
