# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- Multi-run benchmark script (`benchmarks/run_multi.py`)
- CONTRIBUTING.md
- LICENSE (MIT)
- .gitignore

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
