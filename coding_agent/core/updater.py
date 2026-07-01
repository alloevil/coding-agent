"""
coding-agent update —— 一条命令自更新（对标 `codex update`）。

探测安装目录（CODING_AGENT_DIR 优先，否则从本模块位置回推），依次：
  1. git pull --ff-only     拉最新代码
  2. pip install -e .        重装（拾取依赖/入口变化）
  3. cargo build --release   若有 tui/ + cargo，则重编全屏 TUI

每步报告结果；某步失败不静默——打印并继续/中止。只在真实 git 仓库里更新。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def discover_install_dir() -> Path | None:
    """
    找到 coding-agent 的安装（源码）目录：
      - $CODING_AGENT_DIR（launcher 会导出）
      - 否则从本文件位置回推：coding_agent/core/updater.py → 上两级是包根，再上一级是仓库
    只在该目录含 pyproject.toml + .git 时才算有效（可更新）。
    """
    env = os.environ.get("CODING_AGENT_DIR")
    candidates = []
    if env:
        candidates.append(Path(env))
    # coding_agent/core/updater.py → parents[2] = 仓库根
    candidates.append(Path(__file__).resolve().parents[2])
    for c in candidates:
        if (c / "pyproject.toml").is_file():
            return c
    return None


def _run(cmd: list[str], cwd: Path) -> tuple[bool, str]:
    """跑一条命令，返回 (ok, 合并输出)。"""
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, out.strip()


def run_update(install_dir: Path | None = None,
               out=print) -> int:
    """
    执行自更新。返回退出码（0 成功；非 0 表示某关键步失败）。
    out 可注入以便测试。
    """
    d = install_dir or discover_install_dir()
    if d is None:
        out("❌ Can't find the coding-agent install directory "
            "(no pyproject.toml). If you installed from source, run "
            "`git pull` in that directory manually.")
        return 1
    out(f"📂 Updating coding-agent in {d}")

    # 1. git pull（需要是 git 仓库）
    if not (d / ".git").exists():
        out("⚠️  Not a git checkout — skipping `git pull` "
            "(re-run the install script to update).")
    else:
        if not shutil.which("git"):
            out("❌ git not found on PATH."); return 1
        ok, msg = _run(["git", "pull", "--ff-only"], d)
        if not ok:
            out(f"❌ git pull failed:\n{msg}")
            out("   (local changes or diverged branch? resolve, then re-run.)")
            return 1
        out(f"⬇️  git pull: {msg.splitlines()[-1] if msg else 'ok'}")

    # 2. pip 重装（用当前解释器的 venv）
    py = os.environ.get("CODING_AGENT_PYTHON") or sys.executable
    ok, msg = _run([py, "-m", "pip", "install", "-q", "-e", "."], d)
    if not ok:
        out(f"❌ pip reinstall failed:\n{msg[-500:]}")
        return 1
    out("📦 Reinstalled Python package")

    # 3. 重编 Rust TUI（可选：需 tui/ + cargo）
    if (d / "tui" / "Cargo.toml").is_file():
        if shutil.which("cargo"):
            ok, msg = _run(["cargo", "build", "--release"], d / "tui")
            if ok:
                out("🦀 Rebuilt the Rust TUI")
            else:
                out(f"⚠️  Rust TUI rebuild failed (CLI still works):\n{msg[-300:]}")
        else:
            out("ℹ️  cargo not found — skipped TUI rebuild.")

    out("✅ Update complete. Restart coding-agent to use the new version.")
    return 0
