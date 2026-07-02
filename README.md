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

Remote one-liner (clones + installs into an isolated `.venv`, no manual clone needed):

```bash
curl -fsSL https://raw.githubusercontent.com/alloevil/coding-agent/master/install.sh | bash
```

Or clone first, then install:

```bash
git clone https://github.com/alloevil/coding-agent.git
cd coding-agent
./install.sh                 # core + dev deps + full-screen TUI (needs cargo)
# ./install.sh --no-tui      # skip the Rust TUI (no cargo)
# ./install.sh --all         # also tiktoken (token counting) + playwright (browser tools)
```

Or, equivalently, with `make`:

Install into a dedicated virtualenv so the project's pinned deps (httpx, rich)
don't clash with other tools in a shared/global environment:

```bash
# Create an isolated venv and install (with dev deps). Equivalent to:
#   python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
make venv

# Run tests / the agent / the benchmark inside the venv
make test
make run
make bench
```

After installing, just run it — the first launch walks you through setup
(provider, API key, model), no environment variables needed:

```bash
coding-agent            # full-screen TUI (guided setup on first run)
coding-agent --setup    # reconfigure provider / key / model anytime
coding-agent --cli      # force the plain/rich CLI instead of the TUI
```

`install.sh` puts a `coding-agent` launcher on your PATH (`~/.local/bin`). If
that dir isn't on your PATH, the installer prints the one line to add.

> Advanced: you can skip the wizard by exporting `MODEL_API_KEY` /
> `MODEL_BASE_URL` / `MODEL_PRIMARY` (or `OPENAI_API_KEY`).

> Avoid `pip install -e .` into a shared conda/global environment — it will
> upgrade httpx/rich and can break unrelated packages.

### Configuration

```bash
# OpenAI compatible API
export OPENAI_API_KEY="your-key"
export OPENAI_API_BASE="https://api.openai.com/v1"
export CODING_AGENT_MODEL="gpt-4"

# Or any OpenAI-compatible gateway / self-hosted endpoint
export LLM_API_KEY="your-key"
export LLM_BASE_URL="https://your-gateway.example.com/v1"
export CODING_AGENT_MODEL="your-model-name"
```

### Usage

```bash
# Interactive (full-screen TUI when built; CLI otherwise)
coding-agent

# Resume where you left off
coding-agent --continue          # most recent session
coding-agent --resume            # pick from a session list

# Headless one-shot (pipe/CI friendly; final reply on stdout)
coding-agent -p "summarize recent commits"

# One-run config override (not persisted)
coding-agent -c model=gpt-4o -c protocol=anthropic

# Maintenance subcommands
coding-agent config show|get|set|path   # inspect / edit config
coding-agent doctor [--probe] [--json]  # diagnose config & endpoint
coding-agent update                     # pull latest + reinstall + rebuild TUI
```

In the TUI:

| Key / input | Action |
|---|---|
| `/` | slash commands (`/model` `/config` `/help` `/compact` …), Tab-completes |
| `@` | reference a workspace file (fuzzy completion, Tab to accept) |
| `!cmd` | run a shell command directly — output shown inline, visible to the model |
| `Shift+Tab` | toggle auto-accept (⏵⏵ chip when on) |
| `y / n / a` | approve / deny / always-allow when a tool asks for permission |
| `Esc` | interrupt a running turn · clear the draft when idle |
| `Ctrl+L` | clear the screen (session continues) |
| `PgUp/PgDn`, mouse wheel | scroll the transcript |
| `/vim` then `Esc` | opt-in vim modal editing (NORMAL/INSERT chip in the footer) |

### Slash commands

Type `/help` in a session for the live list. All are also Tab-completable.

| Command | What it does |
|---|---|
| `/help` | list all commands |
| `/tools` | list available tools |
| `/model [provider:model]` | show or switch the model / provider |
| `/cost`, `/context` | token usage (with $ estimate) / context-window breakdown |
| `/compact` | summarize & compact the conversation now |
| `/recap` | ask the model to summarize the session so far |
| `/review [focus]` | review the current uncommitted changes |
| `/diff` | show this session's file changes |
| `/undo` | revert the last file change |
| `/plan`, `/plan-mode` | show the plan / toggle read-only planning mode |
| `/memory [add <text>]` | show or append project memory |
| `/export [path]` | export the session to markdown |
| `/agents`, `/agent <name>` | list / switch named agent profiles |
| `/sessions`, `/resume` | pick a past session to resume |
| `/new`, `/clear` | start a fresh session |
| `/mcp`, `/hooks` | list configured MCP servers / lifecycle hooks |
| `/doctor [probe]` | environment health check (probe hits the endpoint) |
| `/permissions [auto\|ask]` | show or set the tool-approval mode |
| `/vim` | toggle vim modal editing (TUI only) |
| `/config`, `/setup`, `/status` | inspect config / re-run the wizard / session status |
| `/init` | generate AGENTS.md from a repo scan |

Custom commands live in `.coding-agent/commands/<name>.md` (a prompt template; `$ARGUMENTS` is substituted).

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

# Run tests with the coverage gate (fails if total < 75%, see pyproject.toml)
make cov

# Run benchmarks
python benchmarks/benchmark.py
```

CI (GitHub Actions) runs the Python suite with the coverage gate and builds +
tests the Rust TUI on every push and PR to `master`.

## 📚 References

- [VILA-Lab/Dive-into-Claude-Code](https://github.com/VILA-Lab/Dive-into-Claude-Code) — Claude Code architecture analysis
- [opencode-ai/opencode](https://github.com/opencode-ai/opencode) → [charmbracelet/crush](https://github.com/charmbracelet/crush) — Go implementation reference
- [XiaomiMiMo/MiMo-Code](https://github.com/XiaomiMiMo/MiMo-Code) — Persistent memory system
- [openai/codex](https://github.com/openai/codex) — Rust implementation reference

## 📄 License

MIT License - see [LICENSE](LICENSE) for details.
