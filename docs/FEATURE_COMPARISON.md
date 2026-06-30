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
| Ask-user / question | ❌ | ✅ (AskUserQuestion) | ✅ question |
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
| Slash commands | ❌ | ✅ (`/init` etc + custom) | ✅ (custom templated) |
| Hooks (lifecycle) | 🟡 (enum exists; only PRE/POST_TOOL fire; no config) | ✅ (settings.json hooks) | ✅ (plugins) |
| Config file load | 🟡 (`from_file` unused) | ✅ (settings.json) | ✅ (opencode.json) |
| TUI | ❌ (CLI + Go protocol) | ✅ | ✅ (rich TUI) |
| Session resume (`--resume`) | 🟡 (load_state, no CLI flag) | ✅ | ✅ |
| Multimodal (images) | ❌ | ✅ | ✅ |
| Cost/token budget stop | ❌ (only max_turns) | ✅ | 🟡 |

## Prioritized backlog (next, all offline-verifiable)

1. **Slash command system** — `/init`, `/compact`, `/cost`, custom commands from a
   commands dir. opencode templates these; Claude Code documents the model. HIGH.
2. **Hooks config-ization** — wire PRE/POST_MODEL_CALL + ON_COMPACT, load hook
   config from settings, so users can register automation. MED.
3. **`ask_user` / question tool** — both peers have it; lets the agent ask a
   structured question instead of guessing. MED.
4. **Config file load** — actually call `AgentConfig.from_file` from
   `~/.config/coding-agent/config.json` + project `.coding-agent.json`. MED.
5. **Session resume UX** — `coding-agent --resume <id>` / list+pick. MED.
6. **Cost/token budget** — stop when a token budget is exceeded (we track usage
   already; just need the gate). MED.
7. **LSP auto-start** — tools exist but no language server is launched by default. LOW.
8. **Multimodal image input** — needs vision-capable endpoint. LOW (blocked on endpoint).
9. **More edit strategies** — opencode has 9 (escape-normalized, trimmed-boundary,
   context-aware, multi-occurrence); we have 5. LOW (diminishing returns).
