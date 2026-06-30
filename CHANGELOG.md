# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- `update_plan` tool (Codex/Claude-Code style step tracking) for multi-step tasks
- Hierarchical project-context loading: `AGENTS.md` / `CLAUDE.md` walked from
  cwd to repo root + a global file, merged into prompt assembly
- Unified async `ModelClient` (httpx) with API-level exponential-backoff retry,
  shared by the CLI and protocol frontends
- Tests for the model client, project context, compaction, and plan tool
- Multi-run benchmark script (`benchmarks/run_multi.py`)
- CONTRIBUTING.md
- LICENSE (MIT)
- .gitignore

### Fixed
- Packaging: replaced nonexistent `setuptools.backends._legacy:_Backend`
  build backend with `setuptools.build_meta`; console script now runs the
  event loop via a sync `cli()` wrapper
- Subagents crashed on run (`object.__new__` skipped `_interrupt_event` /
  `retry_config` / `rollback_log`); now built via a `register_builtin_tools`
  constructor flag
- Context compaction no longer orphans tool results or wipes recent history;
  summarizes the older prefix and keeps recent turns verbatim
- Token estimation now counts tool-call arguments and tool-result content
- `register_*_tools()` accept an explicit registry (testable; no global
  singleton pollution between tests)
- Added missing `AgentConfig.stream` field (latent `AttributeError`)

### Changed
- 5 previously failing tests fixed; suite now 271 passing

## [0.2.0] - 2026-06-29

### Changed
- Improved benchmark pass rate from 62.1% to 93.1%
- Added API exponential backoff retry (3→5 attempts)
- Fixed verify function `_first_pass()` helper
- Relaxed file name matching in verify functions
- Added system prompt hard rules for code generation
- Added `list_files` tool
- Added automatic dependency installation in shell tool

### Fixed
- Fixed `(False, "msg")` being truthy in `or` chains
- Fixed class name matching in verify functions
- Fixed 14 429 rate limit errors

## [0.1.0] - 2026-03-31

### Added
- Initial release
- Agent Loop core implementation
- Tool system (file ops, shell, git)
- Memory system (SQLite)
- Context management (5-layer compression)
- Benchmark suite (58 test cases)
- Basic unit tests
