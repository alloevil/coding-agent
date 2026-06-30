# Feature Comparison: coding-agent vs Claude Code vs opencode

Status snapshot (361 tests passing). Compares our `coding-agent` against:
- **Claude Code** — Anthropic's CLI, by its *publicly documented* features (the
  leaked source is deliberately not consulted).
- **opencode** — sst/opencode, open source (read directly from the repo).

Legend: ✅ have it · 🟡 partial · ❌ missing

## Tools

| Capability | coding-agent | Claude Code | opencode |
|---|---|---|---|
| Read file (paginated) | ✅ `file_read` (2k pages, binary/size guard) | ✅ Read | ✅ read |
| Write file | ✅ `file_write` (+syntax warn) | ✅ Write | ✅ write |
| Edit (fuzzy multi-strategy) | ✅ `file_edit` (5 strategies) | ✅ Edit | ✅ edit (9 strategies) |
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
| LSP (real language servers) | 🟡 `lsp_*` (not started by default) | ✅ (diagnostics) | ✅ full LSP client |
| Skills | ❌ | ✅ Skills | ✅ skill tool |
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
| Slash commands | ✅ (built-in + custom) | ✅ (`/init` etc + custom) | ✅ (custom templated) |
| Hooks (lifecycle) | 🟡 (enum exists; only PRE/POST_TOOL fire; no config) | ✅ (settings.json hooks) | ✅ (plugins) |
| Config file load | ✅ (global + project merge) | ✅ (settings.json) | ✅ (opencode.json) |
| TUI | ❌ (CLI + Go protocol) | ✅ | ✅ (rich TUI) |
| Session resume (`--resume`) | 🟡 (load_state, no CLI flag) | ✅ | ✅ |
| Multimodal (images) | ❌ | ✅ | ✅ |
| Cost/token budget stop | ✅ (max_total_tokens) | ✅ | 🟡 |

## Prioritized backlog

**Done since first matrix:** slash commands ✅, config-file load ✅, token budget ✅,
ask_user ✅, fuzzy edit ✅, persistent shell cwd ✅, post-edit syntax check ✅.

**Remaining (offline-verifiable):**
1. **Hooks config-ization** — wire PRE/POST_MODEL_CALL + ON_COMPACT, load hook
   config from settings so users can register automation. MED.
2. **Session resume UX** — `coding-agent --resume <id>` / list+pick. MED.
3. **LSP auto-start** — tools exist but no language server is launched by default. LOW.
4. **More edit strategies** — opencode has 9; we have 5 (escape-normalized,
   trimmed-boundary, context-aware, multi-occurrence). LOW (diminishing returns).
5. **`/init` command** — generate an AGENTS.md by scanning the repo. MED.

**Blocked on endpoint (can't verify here):**
- Multimodal image input (needs a vision endpoint).
- Anthropic-native backend (mimorouter group unreachable with this token).
