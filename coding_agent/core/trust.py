"""
trust directory —— 首次在某目录运行时询问是否信任（对标 Codex 的 trust screen）。

信任状态存全局 config.json 的 `trusted_dirs` 列表（真实路径）。未信任的目录下，
WRITE/EXECUTE 默认走 ASK（即使 auto_approve），READ 不受限。信任后记录，下次直接放行。

设计取舍：不引入新文件，复用全局 config.json（setup_wizard 的写入真源），
只加一个 trusted_dirs 键。
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _canon(path: str | os.PathLike[str]) -> str:
    """规范化目录路径（解析符号链接 + 绝对化），用于稳定比较/存储。"""
    return str(Path(path).resolve())


def _load(home: str | None = None) -> dict:
    from .setup_wizard import global_config_path
    p = global_config_path(home)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def is_trusted(directory: str | os.PathLike[str], home: str | None = None) -> bool:
    """目录是否已被信任（精确匹配已信任目录，或其子目录）。"""
    target = _canon(directory)
    trusted = _load(home).get("trusted_dirs", []) or []
    for t in trusted:
        tc = _canon(t)
        if target == tc or target.startswith(tc + os.sep):
            return True
    return False


def trust_directory(directory: str | os.PathLike[str], home: str | None = None) -> Path:
    """把目录加入信任列表（合并写入 config.json，不动其它键）。返回配置路径。"""
    from .setup_wizard import global_config_path
    p = global_config_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _load(home)
    trusted = list(data.get("trusted_dirs", []) or [])
    c = _canon(directory)
    if c not in trusted:
        trusted.append(c)
    data["trusted_dirs"] = trusted
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def list_trusted(home: str | None = None) -> list[str]:
    return list(_load(home).get("trusted_dirs", []) or [])
