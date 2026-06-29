# 🧠 Coding Agent

A lightweight, modular AI coding agent framework inspired by [Claude Code](https://github.com/VILA-Lab/Dive-into-Claude-Code) architecture. Built for benchmarking, research, and production use.

## ✨ Features

- **Agent Loop** — AsyncGenerator-based while-loop with streaming support
- **Tool System** — Pluggable tools with hook-based lifecycle (27 events)
- **Context Management** — 5-layer progressive compression (Budget → Snip → Microcompact → Collapse → Auto-Compact)
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

```bash
# Clone the repository
git clone https://github.com/alloevil/coding-agent.git
cd coding-agent

# Install dependencies
pip install -e .

# Or install with dev dependencies
pip install -e ".[dev]"
```

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
| `file_read` | READ | Read file contents |
| `file_write` | WRITE | Create/overwrite files |
| `file_edit` | WRITE | Precise text replacement |
| `file_search` | READ | Find files by pattern |
| `grep` | READ | Search file contents |
| `shell_exec` | EXECUTE | Run shell commands |
| `git_status` | READ | Git status |
| `git_diff` | READ | Git diff |
| `git_commit` | WRITE | Git commit |
| `git_log` | READ | Git log |
| `list_files` | READ | List directory contents |

## 📐 Design Principles

1. **Deny-first** — Default deny, strict rules override宽松规则
2. **Context as scarce resource** — 5-layer compression pipeline
3. **Append-only durable state** — Append-only persistence
4. **Transparent config** — User-visible configuration
5. **Hook system** — 27 lifecycle events, zero-cost injection

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
