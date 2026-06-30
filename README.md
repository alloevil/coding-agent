# 🧠 Coding Agent

A lightweight, modular AI coding agent framework inspired by [Claude Code](https://github.com/VILA-Lab/Dive-into-Claude-Code) architecture. Built for benchmarking, research, and production use.

## ✨ Features

- **Agent Loop** — AsyncGenerator-based while-loop with streaming support
- **Tool System** — Pluggable tools with a hook-based lifecycle (pre/post tool use, model call, error, compact)
- **Context Management** — progressive compaction: per-result truncation (snip) → model summarization of older history while keeping recent turns verbatim → hard budget reduction, all preserving tool-call/result pairing
- **Memory System** — SQLite-based session persistence with project memory
- **Permission System** — Deny-first, gradual trust model
- **Built-in Tools** — File ops, shell, git, LSP, browser, TDD
- **Benchmark Suite** — 58 test cases across 7 categories

## 📊 Benchmark Results

| Metric | Value |
|--------|-------|
| **Total Cases** | 58 |
| **Pass Rate** | 93.1% (54/58) |
| **Easy** | 100% |
| **Medium** | 100% |
| **Hard** | 83-91% (model variance) |

### By Category

| Category | Pass Rate |
|----------|-----------|
| Bug Fix | 10/10 (100%) |
| Code Understanding | 5/5 (100%) |
| File Ops | 8/8 (100%) |
| Multi-Step | 5/6 (83%) |
| Planning | 4/6 (67%) |
| Refactor | 6/6 (100%) |
| Shell | 5/5 (100%) |
| Test Generation | 6/6 (100%) |
| Tool Combo | 5/6 (83%) |

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- httpx
- rich

### Installation

Install into a dedicated virtualenv so the project's pinned deps (httpx, rich)
don't clash with other tools in a shared/global environment:

```bash
# Clone the repository
git clone https://github.com/alloevil/coding-agent.git
cd coding-agent

# Create an isolated venv and install (with dev deps). Equivalent to:
#   python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
make venv

# Run tests / the agent / the benchmark inside the venv
make test
make run
make bench
```

> Avoid `pip install -e .` into a shared conda/global environment — it will
> upgrade httpx/rich and can break unrelated packages.

### Configuration

```bash
# OpenAI compatible API
export OPENAI_API_KEY="your-key"
export OPENAI_API_BASE="https://api.openai.com/v1"
export CODING_AGENT_MODEL="gpt-4"

# Or use Xiaomi mify
export LLM_API_KEY="your-mify-key"
export LLM_BASE_URL="http://model.mify.ai.srv/v1"
export CODING_AGENT_MODEL="xiaomi/mimo-v2.5-pro"
```

### Usage

```bash
# Run the agent
coding-agent

# Or run directly
python -m coding_agent.main
```

## 🏗️ Architecture

```
coding-agent/
├── coding_agent/
│   ├── core/
│   │   ├── agent.py          # Agent Loop core
│   │   ├── state.py          # Agent state management
│   │   └── config.py         # Configuration
│   ├── tools/
│   │   ├── base.py           # Tool base class
│   │   ├── registry.py       # Tool registry
│   │   ├── file_ops.py       # File operations
│   │   ├── shell.py          # Shell execution
│   │   └── git_ops.py        # Git operations
│   ├── memory/
│   │   ├── session.py        # Session persistence (SQLite)
│   │   └── project.py        # Project memory
│   └── main.py               # Entry point
├── benchmarks/
│   └── benchmark.py          # Benchmark suite (58 cases)
├── tests/                    # Unit tests
├── pyproject.toml
└── LICENSE
```

## 🧪 Running Benchmarks

```bash
# Run single benchmark
python benchmarks/benchmark.py

# Run 3x average (eliminates model variance)
python benchmarks/run_multi.py
```

## 🛠️ Built-in Tools

| Tool | Permission | Description |
|------|------------|-------------|
| `file_read` | READ | Read file contents (binary/size guarded) |
| `file_write` | WRITE | Create/overwrite files |
| `file_edit` | WRITE | Precise text replacement (`replace_all` option) |
| `apply_patch` | WRITE | Atomic multi-file add/update/delete patch |
| `file_search` | READ | Find files by glob (skips `.git`/`node_modules`/…) |
| `grep` | READ | Search file contents (skips noise dirs) |
| `list_files` | READ | List directory contents |
| `shell_exec` | EXECUTE | Run shell commands (sandboxed) |
| `git_status` / `git_diff` / `git_log` | READ | Git inspection |
| `git_commit` | WRITE | Git commit |
| `tdd_run_tests` | EXECUTE | Run the project's test suite (auto-detected) |
| `tdd_fix_loop` / `tdd_watch` | EXECUTE | TDD fix loop / watch mode |
| `update_plan` | READ | Track a multi-step plan |
| `memory_save` / `memory_search` / `memory_read` | READ/WRITE | Project memory |
| `agent_spawn` / `agent_parallel` | EXECUTE | Run sub-agents |
| `rollback_last` | WRITE | Undo the last write/edit |

Plus optional LSP and browser tool groups. The agent also loads project
conventions from `AGENTS.md` / `CLAUDE.md` (walked from cwd to repo root).

## 📐 Design Principles

1. **Deny-first** — Default deny; stricter rules override looser ones
2. **Context as scarce resource** — progressive compaction pipeline (see above)
3. **Append-only durable state** — append-only message persistence
4. **Transparent config** — user-visible configuration
5. **Hook system** — lifecycle hooks (pre/post tool, model call, error, compact)

## 🤝 Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

### Development Setup

```bash
# Clone and install
git clone https://github.com/alloevil/coding-agent.git
cd coding-agent
pip install -e ".[dev]"

# Run tests
pytest

# Run benchmarks
python benchmarks/benchmark.py
```

## 📚 References

- [VILA-Lab/Dive-into-Claude-Code](https://github.com/VILA-Lab/Dive-into-Claude-Code) — Claude Code architecture analysis
- [opencode-ai/opencode](https://github.com/opencode-ai/opencode) → [charmbracelet/crush](https://github.com/charmbracelet/crush) — Go implementation reference
- [XiaomiMiMo/MiMo-Code](https://github.com/XiaomiMiMo/MiMo-Code) — Persistent memory system
- [openai/codex](https://github.com/openai/codex) — Rust implementation reference

## 📄 License

MIT License - see [LICENSE](LICENSE) for details.
