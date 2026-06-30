# Feature Comparison: coding-agent vs Claude Code vs opencode

Status snapshot (560 tests passing). Compares our `coding-agent` against:
- **Claude Code** — Anthropic's CLI, by its *publicly documented* features (the
  leaked source is deliberately not consulted).
- **opencode** — sst/opencode, open source (read directly from the repo).

Legend: ✅ have it · 🟡 partial · ❌ missing

## Tools

| Capability | coding-agent | Claude Code | opencode |
|---|---|---|---|
| Read file (paginated) | ✅ `file_read` (2k pages, binary/size guard) | ✅ Read | ✅ read |
| Write file | ✅ `file_write` (+syntax warn) | ✅ Write | ✅ write |
| Edit (fuzzy multi-strategy) | ✅ `file_edit` (7 strategies, returns diff) | ✅ Edit | ✅ edit (9 strategies) |
| Multi-file atomic patch | ✅ `apply_patch` | 🟡 (MultiEdit) | ✅ apply-patch |
| Glob / file search | ✅ `file_search` (gitignore-aware) | ✅ Glob | ✅ glob |
| Grep | ✅ `grep` (gitignore-aware) | ✅ Grep | ✅ grep |
| Shell (persistent cwd) | ✅ `shell_exec` (sandboxed, cwd persists) | ✅ Bash | ✅ bash |
| Web fetch | ✅ `web_fetch` | ✅ WebFetch | ✅ webfetch |
| Web search | 🟡 `web_search` (DDG, not live-verified) | ✅ WebSearch | ✅ websearch |
| Plan / todo | ✅ `update_plan` (re-injected) | ✅ TodoWrite | ✅ todowrite + plan |
| Subagents | ✅ `agent_spawn`/`agent_parallel` | ✅ Task | ✅ task |
| Ask-user / question | ✅ `ask_user` | ✅ (AskUserQuestion) | ✅ question |
| Memory | ✅ `memory_*` (SQLite/project) | ✅ (CLAUDE.md + memory) | 🟡 |
| TDD / test runner | ✅ `tdd_run_tests` | 🟡 (via Bash) | 🟡 (via bash) |
| LSP (real language servers) | ✅ `lsp_*` (lazy-start on first use) | ✅ (diagnostics) | ✅ full LSP client |
| Skills | ✅ `skill` (SKILL.md, progressive disclosure, ~/.claude/skills interop) | ✅ Skills | ✅ skill tool |
| Named custom agents | ✅ (`.coding-agent/agents/*.md`: prompt+model+tools+mode; `/agent` `/agents`) | ✅ subagents | ✅ agent/mode |
| Per-agent tool restriction | ✅ (profile allow/deny; hidden from model + denied at dispatch) | ✅ | ✅ |
| Plan→build handoff | ✅ (one-shot synthetic note on switch) | ✅ | ✅ (reminders) |
| External-dir write guard | ✅ (writes outside workspace root ASK) | 🟡 | ✅ (assertExternalDirectory) |
| Truncated output spill-to-disk | ✅ (full output to temp file + path hint) | 🟡 | ✅ (truncation-dir) |
| Browser control | ✅ `browser_*` (playwright) | 🟡 | ❌ |

## Core / loop

| Capability | coding-agent | Claude Code | opencode |
|---|---|---|---|
| Streaming | ✅ httpx SSE | ✅ | ✅ |
| Parallel read-only tools | ✅ | ✅ | ✅ |
| Context compaction | ✅ (summary + recent-keep) | ✅ (microcompact/auto) | ✅ (compaction + overflow + summary) |
| AGENTS.md / CLAUDE.md | ✅ (hierarchical) | ✅ (CLAUDE.md) | ✅ (AGENTS.md) |
| Plan re-injection | ✅ | ✅ | ✅ |
| Reasoning surfaced | ✅ (reasoning_content + tokens) | ✅ (thinking) | ✅ |
| Prompt caching | ✅ (cache_key + usage) | ✅ | ✅ |
| Retry / backoff | ✅ (transient classify) | ✅ | ✅ retry |
| Interrupt | ✅ | ✅ (Esc) | ✅ |
| Rollback / revert edits | ✅ `rollback_last` | 🟡 | ✅ session revert |
| Permissions (allow/deny/ask) | ✅ rule engine | ✅ (settings perms) | ✅ per-tool permission |
| Multi-provider | 🟡 (OpenAI-compat; Anthropic blocked here) | ✅ (Anthropic) | ✅ (many providers) |
| Session persistence | ✅ SQLite | ✅ | ✅ |
| MCP | ✅ stdio client | ✅ (stdio+SSE) | ✅ (stdio+SSE+oauth) |

## Surface / UX

| Capability | coding-agent | Claude Code | opencode |
|---|---|---|---|
| Plan mode (read-only) | ✅ (`/plan-mode`; denies write/exec) | ✅ (Plan mode) | ✅ (plan agent) |
| Slash commands | ✅ (built-in + custom) | ✅ (`/init` etc + custom) | ✅ (custom templated) |
| Hooks (lifecycle) | ✅ (all events fire; config command hooks) | ✅ (settings.json hooks) | ✅ (plugins) |
| Config file load | ✅ (global + project merge) | ✅ (settings.json) | ✅ (opencode.json) |
| TUI | ✅ (`--tui`: live streaming, in-Live prompts, plan/tools/notice panels) | ✅ | ✅ (rich TUI) |
| Session resume (`--resume`) | ✅ (`--resume`/`--list-sessions`, titled) | ✅ | ✅ |
| Multimodal (images) | ❌ | ✅ | ✅ |
| Cost/token budget stop | ✅ (max_total_tokens) | ✅ | 🟡 |

## Prioritized backlog

**Done since first matrix:** slash commands ✅, config-file load ✅, token budget ✅,
ask_user ✅, fuzzy edit (7 strategies) ✅, persistent shell cwd ✅, post-edit syntax
check ✅, plan mode ✅, ripgrep fast-path ✅, grep context lines ✅, nested AGENTS.md ✅,
`git_branch` ✅, production system prompt ✅, **Skills ✅** (progressive disclosure).

**Remaining (offline-verifiable):**
- (none high-value left — Skills, session titles, plan mode, git_branch, TUI,
  edit-diff, real tokenizer, per-tool timeout all shipped.)

**Depth/quality follow-ups (lower value, deferred):**
- `.gitignore` not truly parsed — only a fixed `DEFAULT_IGNORE_DIRS` set; the
  Python search fallback ignores a project's custom `.gitignore` (ripgrep path
  does honor it). Could add `pathspec` or a simple `.gitignore` line matcher.
- Post-write syntax check is Python-only (`ast.parse`); JSON/YAML are zero-dep
  additions.
- `MAX_SPAWN_DEPTH=1` is hardcoded; could be config-driven.

**Blocked on endpoint (can't verify here):**
- Multimodal image input (needs a vision endpoint).
- Anthropic-native backend (mimorouter group unreachable with this token).
