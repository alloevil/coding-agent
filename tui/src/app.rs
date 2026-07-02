//! Full-screen Ratatui app — Phase 2.
//!
//! Layout: scrollable transcript (top) + bordered input box (bottom) + a
//! one-line status. Pure state (`AppState`) is separated from IO so the
//! event->state mapping is unit-testable; the ratatui render + crossterm
//! keyboard loop live in `run`.

use std::io::Stdout;

use crossterm::event::{DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste,
                       EnableMouseCapture, Event as CtEvent, KeyCode, KeyEventKind,
                       MouseEventKind};
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen,
                          LeaveAlternateScreen};
use crossterm::execute;
use futures::StreamExt;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::{Frame, Terminal};

use crate::backend::Backend;
use crate::composer::Composer;
use crate::proto::{Event, Request};
use crate::setup::{Step, Wizard, PROVIDERS};

/// Resolve the debug-log path from CODING_AGENT_DEBUG.
///
/// - unset / "0" / "false"  → disabled (None)
/// - "1" / "true" / "on"    → default file: state-dir/tui.log
///   ($XDG_STATE_HOME or ~/.local/state)/coding-agent/tui.log
/// - anything else          → treated as an explicit file path
///
/// This fixes the old footgun where `CODING_AGENT_DEBUG=1` created a file
/// literally named `1` in the cwd (the value was used verbatim as a path).
fn debug_log_path() -> Option<std::path::PathBuf> {
    let v = std::env::var("CODING_AGENT_DEBUG").ok()?;
    match v.as_str() {
        "" | "0" | "false" | "off" => None,
        "1" | "true" | "on" => Some(state_dir().join("tui.log")),
        other => Some(std::path::PathBuf::from(other)),
    }
}

/// Writable state dir: $XDG_STATE_HOME/coding-agent or ~/.local/state/coding-agent.
fn state_dir() -> std::path::PathBuf {
    let root = std::env::var("XDG_STATE_HOME")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| {
            let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
            std::path::PathBuf::from(home).join(".local").join("state")
        });
    let d = root.join("coding-agent");
    let _ = std::fs::create_dir_all(&d);
    d
}

/// Cap on the debug log file size before it's rotated (truncated). The log is
/// append-only trace, so a hard cap is enough — we don't keep N generations.
const LOG_MAX_BYTES: u64 = 2 * 1024 * 1024; // 2 MiB

/// Append a debug line to the resolved debug-log path (if enabled).
/// Lets us trace the real execution path on a user's machine.
///
/// Hardening (mirrors Codex's login-log handling):
///  - the file is created 0o600 (it can contain endpoint URLs / backend stderr),
///  - if it grows past LOG_MAX_BYTES it's truncated first, so it can't grow
///    unbounded across many sessions.
pub(crate) fn dbg_log(msg: &str) {
    let Some(path) = debug_log_path() else { return };
    use std::io::Write;

    let mut opts = std::fs::OpenOptions::new();
    opts.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600); // only applies on create
    }
    let Ok(mut f) = opts.open(&path) else { return };

    // Rotate: if the file is already large, truncate before appending.
    if let Ok(meta) = f.metadata() {
        if meta.len() > LOG_MAX_BYTES {
            if let Ok(t) = std::fs::OpenOptions::new().write(true).truncate(true).open(&path) {
                let mut t = t;
                let _ = writeln!(t, "[log truncated at {LOG_MAX_BYTES} bytes]");
            }
        }
    }
    let _ = writeln!(f, "{msg}");
    // Best-effort tighten perms on an already-existing file (create-mode is a
    // no-op when the file predates this hardening).
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
}

/// Example prompts shown on the empty first screen (replicates Codex's
/// PLACEHOLDERS). Guides new users toward useful tasks.
pub const SUGGESTIONS: [&str; 6] = [
    "Explain this codebase",
    "Summarize recent commits",
    "Write tests for <file>",
    "Find and fix a bug in <file>",
    "Run the test suite and fix failures",
    "Use /help to list commands",
];

/// Whether the agent is idle (accepting input) or busy (running a turn).
/// Some variants/methods are part of the status vocabulary but not all are
/// constructed now that tool status is a dynamic string — kept for the label
/// map and future use.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[allow(dead_code)]
pub enum Status {
    Idle,
    Thinking,
    RunningTool,
    Done,
    Error,
}

impl Status {
    pub fn label(&self) -> &'static str {
        match self {
            Status::Idle => "ready",
            Status::Thinking => "thinking…",
            Status::RunningTool => "running tool…",
            Status::Done => "done",
            Status::Error => "error",
        }
    }
    #[allow(dead_code)]
    pub fn is_busy(&self) -> bool {
        matches!(self, Status::Thinking | Status::RunningTool)
    }
}

/// Pure UI state — no IO. Event mapping here is unit-tested.
#[derive(Debug, Default)]
pub struct AppState {
    /// Typed transcript entries (rendered with per-kind styling).
    pub transcript: Vec<crate::render::Entry>,
    /// In-progress streaming assistant text (flushed to transcript on done).
    pub live: String,
    /// In-progress streaming reasoning/thinking text (shown dimmed; cleared when
    /// the answer starts or the turn ends).
    pub live_reasoning: String,
    pub status_str: String,
    pub model: String,
    pub turn: u64,
    pub last_error: Option<String>,
    pub should_quit: bool,
    /// Scroll offset from the bottom (0 = follow tail; N = scrolled up N lines).
    pub scroll: usize,
    /// Backend reported it needs first-run setup (no API key configured).
    pub needs_setup: bool,
    /// Animation tick for the busy spinner (bumped on each redraw while busy).
    pub tick: usize,
    /// Tick value captured when the current turn started, for elapsed display.
    /// The loop ticks ~10/sec while busy, so (tick - busy_start_tick)/10 ≈ secs.
    pub busy_start_tick: usize,
    /// Context usage for the header: prompt+completion tokens and the max window.
    pub used_tokens: u64,
    pub max_context: u64,
    /// Current plan/todo steps (description, status) for the live panel.
    pub plan: Vec<crate::render::PlanStep>,
    /// Indexed workspace files for `@file` completion (scanned once at startup).
    pub files: Vec<String>,
    /// Pending tool-permission request: (tool name, argument summary).
    /// While set, the key handler is modal: y approve / n deny / a always-allow.
    pub pending_permission: Option<(String, String)>,
    /// Pending ask_user question: (question, options). While set, Enter submits
    /// the composer text as the answer (a number picks the matching option).
    pub pending_question: Option<(String, Vec<String>)>,
    /// Active session id (set when resuming via --continue, or from
    /// session_state events). Sent with every user turn so the backend loads it.
    pub session_id: Option<String>,
    /// --continue: adopt the most recent session once sessions_list arrives.
    pub want_continue: bool,
    /// --resume: open a session picker when sessions_list arrives.
    pub want_resume: bool,
    /// Active session-picker modal: (sessions as (id, label), selected index).
    pub session_picker: Option<(Vec<(String, String)>, usize)>,
    /// Current permission mode (Shift+Tab toggles; mirrors backend config).
    pub auto_approve: bool,
    /// True right after an idle Esc with empty input — the next Esc rewinds
    /// (Esc-Esc). Reset by any other key.
    pub esc_armed: bool,
    /// Text returned by a rewind: the run loop moves it into the composer.
    pub rewound_text: Option<String>,
}

impl AppState {
    pub fn new() -> Self {
        let mut s = AppState {
            status_str: "ready".into(),
            ..Default::default()
        };
        // Welcome / getting-started notice so the transcript isn't blank on entry.
        s.transcript.push(crate::render::Entry::Notice(
            "👋 Welcome to coding-agent. Type a task and press Enter. \
             / for commands · ↑↓ history · Ctrl-C to quit.".into()));
        s
    }

    pub fn push_user(&mut self, text: &str) {
        self.transcript.push(crate::render::Entry::User(text.to_string()));
    }

    /// Apply one protocol event to the state. Returns true if the turn ended.
    pub fn apply(&mut self, ev: &Event) -> bool {
        use crate::render::Entry;
        match ev.kind.as_str() {
            "ready" => {
                self.model = ev.str_field("model").unwrap_or("").to_string();
                self.status_str = Status::Idle.label().into();
                self.needs_setup = ev.rest.get("needs_setup")
                    .and_then(|v| v.as_bool()).unwrap_or(false);
                self.max_context = ev.rest.get("max_context_tokens")
                    .and_then(|v| v.as_u64()).unwrap_or(0);
                self.auto_approve = ev.rest.get("auto_approve")
                    .and_then(|v| v.as_bool()).unwrap_or(false);
            }
            "config_updated" => {
                if let Some(v) = ev.rest.get("auto_approve").and_then(|v| v.as_bool()) {
                    self.auto_approve = v;
                }
            }
            "model_changed" => {
                if let Some(m) = ev.str_field("model") {
                    self.model = m.to_string();
                }
            }
            "command_result" => {
                if let Some(t) = ev.str_field("text") {
                    self.transcript.push(Entry::Notice(t.to_string()));
                }
            }
            "plan" => {
                // Live plan/todo panel. Steps: [{step, status}].
                if let Some(arr) = ev.rest.get("steps").and_then(|v| v.as_array()) {
                    self.plan = arr.iter().filter_map(|s| {
                        let desc = s.get("step").and_then(|v| v.as_str())?;
                        let status = s.get("status").and_then(|v| v.as_str()).unwrap_or("pending");
                        Some((desc.to_string(), status.to_string()))
                    }).collect();
                }
            }
            "shell_output" => {
                // `!command` passthrough result: show as a tool-style entry.
                let cmd = ev.str_field("command").unwrap_or("?").to_string();
                let out = ev.str_field("output").unwrap_or("").to_string();
                self.transcript.push(Entry::ToolCall {
                    name: "!".into(), target: cmd });
                self.transcript.push(Entry::ToolResult { ok: true, body: out });
                self.status_str = Status::Idle.label().into();
                return true; // ends the "turn"
            }
            "permission_request" => {
                // Tool wants approval: capture name + a compact argument summary
                // and switch the key handler into modal y/n/a mode.
                let name = ev.str_field("tool_name").or_else(|| ev.str_field("name"))
                    .unwrap_or("?").to_string();
                // Prefer the salient argument (command/path) over raw JSON.
                let args = ev.rest.get("arguments")
                    .map(|v| {
                        let salient = v.get("command").or_else(|| v.get("path"))
                            .or_else(|| v.get("file_path"))
                            .and_then(|x| x.as_str())
                            .map(|s| s.to_string());
                        let s = salient.unwrap_or_else(|| v.to_string());
                        if s.chars().count() > 160 {
                            let cut: String = s.chars().take(160).collect();
                            format!("{cut}…")
                        } else { s }
                    })
                    .unwrap_or_default();
                // Edit/write tools: show a diff preview in the transcript so the
                // user sees WHAT they're approving (Claude Code behavior).
                if let Some(a) = ev.rest.get("arguments") {
                    let preview = if name == "file_edit" {
                        match (a.get("old_text").and_then(|v| v.as_str()),
                               a.get("new_text").and_then(|v| v.as_str())) {
                            (Some(old), Some(new)) => {
                                let mut d = String::new();
                                for l in old.lines().take(8) { d.push_str(&format!("-{l}\n")); }
                                for l in new.lines().take(8) { d.push_str(&format!("+{l}\n")); }
                                Some(d)
                            }
                            _ => None,
                        }
                    } else if name == "file_write" {
                        a.get("content").and_then(|v| v.as_str()).map(|c| {
                            let mut d = String::new();
                            for l in c.lines().take(8) { d.push_str(&format!("+{l}\n")); }
                            if c.lines().count() > 8 { d.push_str("+…\n"); }
                            d
                        })
                    } else {
                        None
                    };
                    if let Some(d) = preview {
                        self.transcript.push(Entry::ToolCall {
                            name: format!("{name}?"), target: args.clone() });
                        self.transcript.push(Entry::ToolResult { ok: true, body: d });
                    }
                }
                self.pending_permission = Some((name, args));
                self.status_str = "awaiting approval".into();
            }
            "question" => {
                // ask_user tool: show the question + numbered options and enter
                // answer mode (Enter submits the composer text; a number picks).
                let q = ev.str_field("question").unwrap_or("?").to_string();
                let opts: Vec<String> = ev.rest.get("options")
                    .and_then(|v| v.as_array())
                    .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
                    .unwrap_or_default();
                let mut text = format!("❓ {q}");
                for (i, o) in opts.iter().enumerate() {
                    text.push_str(&format!("\n   {}. {o}", i + 1));
                }
                self.transcript.push(Entry::Notice(text));
                self.pending_question = Some((q, opts));
                self.status_str = "awaiting answer".into();
            }
            "rewound" => {
                // Esc-Esc rewind: backend popped the last turn. Mirror it in the
                // transcript (drop entries back through the last User entry) and
                // stage the original text for the composer.
                let text = ev.str_field("text").unwrap_or("").to_string();
                if !text.is_empty() {
                    if let Some(pos) = self.transcript.iter()
                        .rposition(|e| matches!(e, Entry::User(_))) {
                        self.transcript.truncate(pos);
                    }
                    self.turn = self.turn.saturating_sub(1);
                    self.rewound_text = Some(text);
                    self.transcript.push(Entry::Notice(
                        "⎌ Rewound last turn — edit and resend.".into()));
                }
            }
            "sessions_list" => {
                // --continue: adopt the most recent session (list is sorted by
                // updated_at DESC). Show a resume notice with its title if any.
                if self.want_continue {
                    self.want_continue = false;
                    if let Some(first) = ev.rest.get("sessions")
                        .and_then(|v| v.as_array()).and_then(|a| a.first()) {
                        if let Some(id) = first.get("id").and_then(|v| v.as_str()) {
                            self.session_id = Some(id.to_string());
                            let title = first.get("metadata")
                                .and_then(|m| m.get("title")).and_then(|v| v.as_str())
                                .unwrap_or("");
                            let short: String = id.chars().take(8).collect();
                            self.transcript.push(Entry::Notice(if title.is_empty() {
                                format!("↩ Resumed session {short}…")
                            } else {
                                format!("↩ Resumed session {short}… — {title}")
                            }));
                        }
                    }
                } else if self.want_resume {
                    // --resume: open the picker (↑↓ select, Enter adopt, Esc new).
                    self.want_resume = false;
                    let items: Vec<(String, String)> = ev.rest.get("sessions")
                        .and_then(|v| v.as_array())
                        .map(|a| a.iter().filter_map(|s| {
                            let id = s.get("id").and_then(|v| v.as_str())?;
                            let title = s.get("metadata")
                                .and_then(|m| m.get("title")).and_then(|v| v.as_str())
                                .unwrap_or("(untitled)");
                            let updated = s.get("updated_at").and_then(|v| v.as_str())
                                .unwrap_or("");
                            let short: String = id.chars().take(8).collect();
                            Some((id.to_string(), format!("{short}…  {title}  {updated}")))
                        }).collect())
                        .unwrap_or_default();
                    if items.is_empty() {
                        self.transcript.push(Entry::Notice(
                            "No sessions to resume — starting fresh.".into()));
                    } else {
                        self.session_picker = Some((items, 0));
                        self.status_str = "pick a session".into();
                    }
                }
            }
            "config_saved" => {
                self.needs_setup = false;
                if let Some(m) = ev.str_field("model") {
                    self.model = m.to_string();
                }
                self.transcript.push(Entry::Notice(
                    "🧩 Configuration saved. You're ready to go!".into()));
            }
            "thinking" => {
                self.status_str = Status::Thinking.label().into();
                if let Some(t) = ev.rest.get("turn").and_then(|v| v.as_u64()) {
                    self.turn = t;
                }
                self.last_error = None;
                self.live_reasoning.clear();
            }
            "stream_reasoning" => {
                // Reasoning/thinking tokens (gpt-5/o, DeepSeek reasoning_content).
                if let Some(t) = ev.str_field("text") {
                    self.live_reasoning.push_str(t);
                }
                self.status_str = Status::Thinking.label().into();
            }
            "stream_text" => {
                if let Some(t) = ev.str_field("text") {
                    self.live.push_str(t);
                }
                // The answer has started — reasoning is done; drop it so it
                // doesn't linger above the reply.
                self.live_reasoning.clear();
                self.status_str = Status::Thinking.label().into();
            }
            "assistant_message" => {
                // Prefer streamed text; fall back to the full content.
                let final_text = if !self.live.is_empty() {
                    std::mem::take(&mut self.live)
                } else {
                    ev.str_field("content").unwrap_or("").to_string()
                };
                self.live_reasoning.clear();
                if !final_text.is_empty() {
                    self.transcript.push(Entry::Agent(final_text));
                }
            }
            "tool_call" => {
                let name = ev.str_field("name").unwrap_or("?").to_string();
                // Name the active tool in the status line (Claude Code shows
                // what it's doing, e.g. "running shell_exec…").
                self.status_str = format!("running {name}…");
                // Surface a target (path / command) from the call arguments for
                // a more informative one-liner (e.g. "file_edit  src/main.py").
                let target = ev.rest.get("arguments")
                    .and_then(|a| a.get("path").or_else(|| a.get("command")).or_else(|| a.get("file_path")))
                    .and_then(|v| v.as_str())
                    .unwrap_or("").to_string();
                self.transcript.push(Entry::ToolCall { name, target });
            }
            "tool_result" => {
                let ok = !ev.rest.get("is_error").and_then(|v| v.as_bool()).unwrap_or(false);
                // The result body may contain a unified diff (edit/patch tools);
                // render_diff_or_text colors it, or dims plain text. Only show a
                // compact body — skip very long results to keep the transcript clean.
                let body = ev.str_field("result")
                    .or_else(|| ev.str_field("diff"))
                    .unwrap_or("");
                let body = summarize_result_body(body);
                self.transcript.push(Entry::ToolResult { ok, body });
            }
            "error" => {
                self.status_str = Status::Error.label().into();
                self.last_error = Some(ev.str_field("error").unwrap_or("error").to_string());
            }
            "compacting" => self.status_str = "compacting…".into(),
            "retrying" => self.status_str = "retrying…".into(),
            "done" | "session_state" => {
                // Flush any trailing live text, mark idle, signal turn end.
                if !self.live.is_empty() {
                    let t = std::mem::take(&mut self.live);
                    self.transcript.push(Entry::Agent(t));
                }
                self.live_reasoning.clear();
                // Track the active session so later turns continue it.
                if let Some(sid) = ev.str_field("session_id") {
                    self.session_id = Some(sid.to_string());
                }
                // Update context usage for the header + a per-turn summary line
                // (Claude Code shows a light stat after each turn).
                if let Some(m) = ev.rest.get("max_context_tokens").and_then(|v| v.as_u64()) {
                    if m > 0 { self.max_context = m; }
                }
                let p = ev.rest.get("prompt_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
                let c = ev.rest.get("completion_tokens").and_then(|v| v.as_u64()).unwrap_or(0);
                if p + c > 0 {
                    self.used_tokens = p + c;
                    let elapsed = self.elapsed_secs();
                    let ctx = if self.max_context > 0 {
                        format!("{}/{} ctx",
                            crate::render::fmt_tokens(self.used_tokens),
                            crate::render::fmt_tokens(self.max_context))
                    } else {
                        format!("{} ctx", crate::render::fmt_tokens(self.used_tokens))
                    };
                    // Dollar estimate when the backend can price the model.
                    let cost = ev.rest.get("cost_usd").and_then(|v| v.as_f64())
                        .map(|d| format!(" · ${d:.4}"))
                        .unwrap_or_default();
                    self.transcript.push(Entry::Notice(format!(
                        "✓ turn {} · {} · {}{}",
                        self.turn, ctx,
                        crate::render::fmt_elapsed_compact(elapsed), cost)));
                }
                self.status_str = Status::Idle.label().into();
                return true;
            }
            _ => {}
        }
        false
    }

    /// Fully-rendered transcript as styled lines (committed entries + the
    /// in-progress live stream + any trailing error). `busy` shows a thinking
    /// indicator when a turn is running but no text has streamed yet.
    pub fn render_lines(&self, busy: bool, width: u16) -> Vec<Line<'static>> {
        use crate::render::{render_entry, render_markdown, spinner_frame, welcome_banner, Entry};
        let mut out: Vec<Line<'static>> = Vec::new();
        for e in &self.transcript {
            out.extend(render_entry(e));
        }
        // First-screen: animated ASCII welcome banner + example prompts, shown
        // while there's no real conversation yet. Replicates Codex's welcome.
        let has_convo = self.transcript.iter()
            .any(|e| matches!(e, Entry::User(_) | Entry::Agent(_)));
        if !has_convo && !busy && self.live.is_empty() {
            let mut banner = welcome_banner(self.tick, width);
            // Put the banner at the very top, above the welcome notice line.
            banner.extend(std::mem::take(&mut out));
            out = banner;
            out.push(Line::from(""));
            out.push(Line::from(Span::styled("  Try:",
                Style::default().fg(Color::DarkGray).add_modifier(Modifier::BOLD))));
            for s in SUGGESTIONS {
                out.push(Line::from(vec![
                    Span::styled("  › ", Style::default().fg(Color::Cyan)),
                    Span::styled(s.to_string(), Style::default().fg(Color::DarkGray)),
                ]));
            }
        }
        // Live reasoning (thinking tokens) — shown dimmed/italic under a
        // "thinking" header while it streams, before the answer text.
        if !self.live_reasoning.is_empty() && self.live.is_empty() {
            out.push(Line::from(Span::styled(
                format!("{} thinking", spinner_frame(self.tick)),
                Style::default().fg(Color::Magenta).add_modifier(Modifier::DIM | Modifier::BOLD))));
            for raw in self.live_reasoning.lines() {
                out.push(Line::from(Span::styled(
                    format!("  {raw}"),
                    Style::default().fg(Color::DarkGray).add_modifier(Modifier::ITALIC))));
            }
        }
        if !self.live.is_empty() {
            // Live streaming agent text with a cursor.
            out.push(Line::from(Span::styled(
                "▌Agent", Style::default().fg(Color::White).add_modifier(Modifier::BOLD))));
            let mut md = render_markdown(&self.live);
            // Append cursor to the last live line.
            if let Some(last) = md.last_mut() {
                last.spans.push(Span::raw("▌"));
            }
            out.extend(md);
        } else if busy && self.live_reasoning.is_empty() {
            // Thinking: model is working but hasn't streamed text/reasoning yet.
            let label = if self.status_str.contains("tool") {
                self.status_str.clone()
            } else {
                "thinking…".to_string()
            };
            out.push(Line::from(Span::styled(
                format!("{} {}", spinner_frame(self.tick), label),
                Style::default().fg(Color::Yellow).add_modifier(Modifier::DIM),
            )));
        }
        if let Some(e) = &self.last_error {
            out.push(Line::from(Span::styled(
                format!("❌ {e}"), Style::default().fg(Color::Red))));
        }
        // Session picker (--resume): list with a highlighted selection.
        if let Some((items, sel)) = &self.session_picker {
            out.push(Line::from(""));
            out.push(Line::from(Span::styled(
                " Resume which session?  (↑↓ select · ⏎ resume · Esc new)",
                Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD))));
            for (i, (_, label)) in items.iter().enumerate() {
                let (marker, style) = if i == *sel {
                    ("▶ ", Style::default().fg(Color::Green).add_modifier(Modifier::BOLD))
                } else {
                    ("  ", Style::default().fg(Color::DarkGray))
                };
                out.push(Line::from(Span::styled(format!(" {marker}{label}"), style)));
            }
        }
        out
    }

    /// Seconds elapsed in the current turn (derived from the 10 Hz busy tick).
    pub fn elapsed_secs(&self) -> u64 {
        (self.tick.saturating_sub(self.busy_start_tick) / 10) as u64
    }

    /// Scroll up by n lines (toward older). total = total lines, view = visible rows.
    pub fn scroll_up(&mut self, n: usize, total: usize, view: usize) {
        let max = total.saturating_sub(view);
        self.scroll = (self.scroll + n).min(max);
    }

    /// Scroll down by n lines (toward newer / tail).
    pub fn scroll_down(&mut self, n: usize) {
        self.scroll = self.scroll.saturating_sub(n);
    }

    /// Given total lines and visible rows, the first line index to render,
    /// honoring the scroll offset (0 = follow tail).
    pub fn view_start(&self, total: usize, view: usize) -> usize {
        let tail_start = total.saturating_sub(view);
        tail_start.saturating_sub(self.scroll)
    }
}

/// Trim a tool-result body for the transcript: keep unified diffs (they're the
/// point of the rich view) but cap plain text to a few lines so a huge stdout
/// dump doesn't flood the conversation.
fn summarize_result_body(body: &str) -> String {
    let body = body.trim_end();
    if body.is_empty() {
        return String::new();
    }
    let is_diff = body.lines().any(|l| l.starts_with("@@") || l.starts_with("+++")
        || l.starts_with("---"));
    if is_diff {
        // diffs: keep up to ~24 lines
        let lines: Vec<&str> = body.lines().take(24).collect();
        let mut s = lines.join("\n");
        if body.lines().count() > 24 { s.push_str("\n     … (diff truncated)"); }
        s
    } else {
        // plain text: first 3 lines only
        let lines: Vec<&str> = body.lines().take(3).collect();
        let mut s = lines.join("\n");
        if body.lines().count() > 3 { s.push_str("\n     …"); }
        s
    }
}

/// Run the full-screen TUI event loop. Drives the backend and renders state.
pub async fn run(mut backend: Backend, model_hint: String, force_setup: bool,
                 want_continue: bool, want_resume: bool) -> std::io::Result<()> {
    // A full-screen TUI needs a real interactive terminal. When stdout/stdin
    // isn't a TTY (piped, non-interactive shell, some IDE terminals), don't
    // fail with a cryptic raw-mode OS error — exit clearly so the launcher can
    // fall back to the CLI.
    use crossterm::tty::IsTty;
    let out_tty = std::io::stdout().is_tty();
    let in_tty = std::io::stdin().is_tty();
    dbg_log(&format!("run start: force_setup={force_setup} stdout_tty={out_tty} stdin_tty={in_tty}"));
    if !out_tty || !in_tty {
        dbg_log("EXIT: not a tty -> exit 3");
        eprintln!("coding-agent: not an interactive terminal — use `coding-agent --cli`, \
                   or run in a real terminal for the full-screen TUI.");
        backend.shutdown().await;
        std::process::exit(3); // distinct code so the launcher can route to CLI
    }

    enable_raw_mode()?;
    let mut stdout = std::io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableBracketedPaste, EnableMouseCapture)?;
    let mut term = Terminal::new(CrosstermBackend::new(stdout))?;

    let mut state = AppState::new();
    state.model = model_hint;
    // Index workspace files once for @file completion (cheap, bounded).
    state.files = crate::file_index::scan(std::path::Path::new("."));
    // --continue: ask for recent sessions; sessions_list adopts the latest.
    // --resume: ask for the list; sessions_list opens the picker instead.
    if want_continue || want_resume {
        state.want_continue = want_continue;
        state.want_resume = want_resume && !want_continue;
        backend.send(&Request::ListSessions).await?;
    }
    let mut composer = Composer::new();
    let mut turn_running = false;
    // --setup forces the wizard open immediately; otherwise it opens when the
    // backend reports needs_setup via the ready event.
    let mut wizard: Option<Wizard> = if force_setup { Some(Wizard::new()) } else { None };

    let mut keys = crossterm::event::EventStream::new();

    let result = run_loop(&mut term, &mut backend, &mut state, &mut composer,
                          &mut turn_running, &mut wizard, &mut keys).await;

    // Restore terminal no matter what.
    disable_raw_mode().ok();
    execute!(term.backend_mut(), LeaveAlternateScreen, DisableBracketedPaste,
             DisableMouseCapture).ok();
    term.show_cursor().ok();
    backend.shutdown().await;
    result
}

async fn run_loop(
    term: &mut Terminal<CrosstermBackend<Stdout>>,
    backend: &mut Backend,
    state: &mut AppState,
    composer: &mut Composer,
    turn_running: &mut bool,
    wizard: &mut Option<Wizard>,
    keys: &mut crossterm::event::EventStream,
) -> std::io::Result<()> {
    loop {
        if let Some(w) = wizard.as_ref() {
            term.draw(|f| render_wizard(f, w))?;
        } else {
            term.draw(|f| render(f, state, composer, *turn_running))?;
        }
        if state.should_quit {
            return Ok(());
        }

        // While a turn is running, tick a spinner ~every 100ms so the busy
        // animation advances even when no backend/key event arrives.
        let tick = tokio::time::sleep(std::time::Duration::from_millis(100));

        tokio::select! {
            _ = tick, if *turn_running && wizard.is_none() => {
                state.tick = state.tick.wrapping_add(1);
            }
            // Backend event
            maybe_ev = backend.events.recv() => {
                match maybe_ev {
                    Some(ev) => {
                        dbg_log(&format!("event: {} (needs_setup before apply={})", ev.kind, state.needs_setup));
                        let ended = state.apply(&ev);
                        if ended {
                            *turn_running = false;
                        }
                        // Rewind: move the popped user text into the composer.
                        if let Some(t) = state.rewound_text.take() {
                            composer.clear();
                            composer.insert_str(&t);
                        }
                        // Open the wizard when the backend asks for setup.
                        if state.needs_setup && wizard.is_none() {
                            dbg_log("-> opening wizard (needs_setup && wizard none)");
                            *wizard = Some(Wizard::new());
                        }
                    }
                    None => { dbg_log("EXIT: backend channel closed"); return Ok(()); }
                }
            }
            // Keyboard event
            maybe_key = keys.next() => {
                if let Some(Ok(ct)) = maybe_key {
                    if wizard.is_some() {
                        handle_wizard_key(ct, backend, state, wizard).await?;
                    } else {
                        handle_key(ct, backend, state, composer, turn_running).await?;
                    }
                }
            }
        }
    }
}

/// Handle a key while the setup wizard is active.
async fn handle_wizard_key(
    ct: CtEvent,
    backend: &mut Backend,
    state: &mut AppState,
    wizard: &mut Option<Wizard>,
) -> std::io::Result<()> {
    let w = match wizard.as_mut() {
        Some(w) => w,
        None => return Ok(()),
    };
    // Bracketed paste (e.g. pasting an API key) — insert into the active text
    // field. Without this, paste events were silently dropped and the key
    // "couldn't be pasted" in the wizard.
    if let CtEvent::Paste(s) = &ct {
        if let Some(field) = w.active_field() {
            field.insert_str(s);
        }
        return Ok(());
    }
    if let CtEvent::Key(k) = ct {
        if k.kind == KeyEventKind::Release {
            return Ok(());
        }
        use crossterm::event::KeyModifiers as M;
        match (k.code, k.modifiers) {
            (KeyCode::Char('c'), M::CONTROL) => state.should_quit = true,
            (KeyCode::Up, _) if w.step == Step::Provider => w.provider_up(),
            (KeyCode::Down, _) if w.step == Step::Provider => w.provider_down(),
            (KeyCode::Char(' '), _) if w.step == Step::AutoApprove => {
                w.auto_approve = !w.auto_approve;
            }
            (KeyCode::Enter, _) => {
                // API key is required — don't advance past the Key step while empty
                // (otherwise we'd save a blank key and reopen the wizard forever).
                if w.step == Step::Key && w.key.text().trim().is_empty() {
                    // ignore Enter; stay on the Key step
                } else {
                    let done = w.advance();
                    if done {
                        let answers = w.answers();
                        backend.send(&Request::SaveConfig { answers }).await?;
                        *wizard = None;
                        // Clear needs_setup now so the loop guard can't reopen the wizard
                        // before the config_saved event round-trips.
                        state.needs_setup = false;
                    }
                }
            }
            (KeyCode::Char(c), _) => {
                if let Some(field) = w.active_field() {
                    field.insert(c);
                }
            }
            (KeyCode::Backspace, _) => {
                if let Some(field) = w.active_field() {
                    field.backspace();
                }
            }
            _ => {}
        }
    }
    Ok(())
}

async fn handle_key(
    ct: CtEvent,
    backend: &mut Backend,
    state: &mut AppState,
    composer: &mut Composer,
    turn_running: &mut bool,
) -> std::io::Result<()> {
    match ct {
        CtEvent::Paste(s) => composer.insert_str(&s),
        CtEvent::Mouse(m) => {
            // Wheel scroll over the transcript (3 lines per notch).
            match m.kind {
                MouseEventKind::ScrollUp => {
                    let total = state.render_lines(*turn_running, 80).len();
                    state.scroll_up(3, total, 10);
                }
                MouseEventKind::ScrollDown => state.scroll_down(3),
                _ => {}
            }
        }
        CtEvent::Key(k) if k.kind != KeyEventKind::Release => {
            use crossterm::event::KeyModifiers as M;
            // Modal: session picker (--resume) — ↑↓ select, Enter adopt, Esc fresh.
            if let Some((items, sel)) = state.session_picker.clone() {
                match k.code {
                    KeyCode::Up => {
                        if let Some(p) = state.session_picker.as_mut() {
                            p.1 = p.1.saturating_sub(1);
                        }
                    }
                    KeyCode::Down => {
                        if let Some(p) = state.session_picker.as_mut() {
                            p.1 = (p.1 + 1).min(items.len().saturating_sub(1));
                        }
                    }
                    KeyCode::Enter => {
                        if let Some((id, label)) = items.get(sel) {
                            state.session_id = Some(id.clone());
                            state.transcript.push(crate::render::Entry::Notice(
                                format!("↩ Resumed session {label}")));
                        }
                        state.session_picker = None;
                        state.status_str = Status::Idle.label().into();
                    }
                    KeyCode::Esc => {
                        state.session_picker = None;
                        state.status_str = Status::Idle.label().into();
                        state.transcript.push(crate::render::Entry::Notice(
                            "Starting a fresh session.".into()));
                    }
                    KeyCode::Char('c') if k.modifiers.contains(M::CONTROL) => {
                        state.should_quit = true;
                    }
                    _ => {}
                }
                return Ok(());
            }
            // Modal: a tool is awaiting approval — y approve / n (or Esc) deny /
            // a approve + auto-approve for the rest of the session.
            if state.pending_permission.is_some() {
                match k.code {
                    KeyCode::Char('y') | KeyCode::Char('Y') | KeyCode::Enter => {
                        state.pending_permission = None;
                        backend.send(&Request::PermissionResponse { approved: true }).await?;
                    }
                    KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc => {
                        state.pending_permission = None;
                        backend.send(&Request::PermissionResponse { approved: false }).await?;
                    }
                    KeyCode::Char('a') | KeyCode::Char('A') => {
                        state.pending_permission = None;
                        backend.send(&Request::SetAutoApprove { value: true }).await?;
                        backend.send(&Request::PermissionResponse { approved: true }).await?;
                    }
                    KeyCode::Char('c') if k.modifiers.contains(M::CONTROL) => {
                        state.should_quit = true;
                    }
                    _ => {} // ignore everything else while modal
                }
                return Ok(());
            }
            // Modal: ask_user question — type freely; Enter submits the answer
            // (a bare number picks the matching option). Backend is blocked on
            // question_response, so we must always send one.
            if let Some((_q, opts)) = state.pending_question.clone() {
                match (k.code, k.modifiers) {
                    (KeyCode::Char('c'), M::CONTROL) => state.should_quit = true,
                    (KeyCode::Enter, _) => {
                        let raw = composer.take();
                        let ans = raw.trim().to_string();
                        // number → option text (1-based), like the CLI
                        let resolved = ans.parse::<usize>().ok()
                            .filter(|n| *n >= 1 && *n <= opts.len())
                            .map(|n| opts[n - 1].clone())
                            .unwrap_or(ans);
                        state.pending_question = None;
                        state.status_str = Status::Thinking.label().into();
                        state.transcript.push(crate::render::Entry::User(
                            format!("(answer) {resolved}")));
                        backend.send(&Request::QuestionResponse { answer: resolved }).await?;
                    }
                    (KeyCode::Char(c), _) => composer.insert(c),
                    (KeyCode::Backspace, _) => composer.backspace(),
                    (KeyCode::Left, _) => composer.left(),
                    (KeyCode::Right, _) => composer.right(),
                    _ => {}
                }
                return Ok(());
            }
            // Any key other than Esc disarms the pending Esc-Esc rewind.
            if !matches!(k.code, KeyCode::Esc) {
                state.esc_armed = false;
            }
            match (k.code, k.modifiers) {
                (KeyCode::Char('c'), M::CONTROL) => state.should_quit = true,
                (KeyCode::Esc, _) => {
                    // Running: interrupt. Idle+draft: clear draft. Idle+empty:
                    // first Esc arms, second Esc (Esc-Esc) rewinds the last turn.
                    if *turn_running {
                        backend.send(&Request::Interrupt).await?;
                    } else if !composer.is_empty() {
                        composer.clear(); // discard draft, no history entry
                        state.esc_armed = false;
                    } else if state.esc_armed {
                        state.esc_armed = false;
                        backend.send(&Request::Rewind).await?;
                    } else {
                        state.esc_armed = true;
                        state.scroll = 0;
                    }
                }
                (KeyCode::Char('l'), M::CONTROL) => {
                    // Ctrl+L: clear the visible transcript (session continues).
                    state.transcript.clear();
                    state.scroll = 0;
                }
                (KeyCode::Enter, m) if m.contains(M::SHIFT) || m.contains(M::ALT) => {
                    composer.newline(); // multi-line input
                }
                (KeyCode::Enter, _) => {
                    if !*turn_running {
                        let text = composer.take();
                        if text.trim().is_empty() {
                            // nothing to send
                        } else if text.trim() == "/sessions" {
                            // Open the session picker live (not just at launch).
                            state.want_resume = true;
                            backend.send(&Request::ListSessions).await?;
                        } else {
                            state.push_user(&text);
                            state.scroll = 0; // follow tail on new input
                            state.busy_start_tick = state.tick; // start elapsed clock
                            backend.send(&Request::UserInput { content: text, session_id: state.session_id.clone() }).await?;
                            *turn_running = true;
                        }
                    }
                }
                (KeyCode::BackTab, _) => {
                    // Shift+Tab: toggle auto-approve (Claude Code's signature).
                    let next = !state.auto_approve;
                    state.auto_approve = next; // optimistic; config_updated confirms
                    backend.send(&Request::SetAutoApprove { value: next }).await?;
                }
                (KeyCode::Tab, _) => {
                    // @file completion takes priority when an @token is active;
                    // otherwise fall back to slash-command completion.
                    if let Some(tok) = composer.at_token() {
                        let hits = crate::file_index::fuzzy_match(&state.files, &tok, 1);
                        if let Some(path) = hits.first() {
                            composer.complete_at(path);
                        }
                    } else {
                        composer.complete_slash();
                    }
                }
                (KeyCode::Up, _) => composer.history_prev(),
                (KeyCode::Down, _) => composer.history_next(),
                (KeyCode::PageUp, _) => {
                    let total = state.render_lines(*turn_running, 80).len();
                    state.scroll_up(10, total, 10); // conservative page; clamps in render
                }
                (KeyCode::PageDown, _) => state.scroll_down(10),
                (KeyCode::Char(c), _) => composer.insert(c),
                (KeyCode::Backspace, _) => composer.backspace(),
                (KeyCode::Delete, _) => composer.delete(),
                (KeyCode::Left, _) => composer.left(),
                (KeyCode::Right, _) => composer.right(),
                (KeyCode::Home, _) => composer.home(),
                (KeyCode::End, _) => composer.end(),
                _ => {}
            }
        }
        _ => {}
    }
    Ok(())
}

/// Render the full-screen setup wizard.
fn render_wizard(f: &mut Frame, w: &Wizard) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(2),  // title
            Constraint::Min(6),     // body
            Constraint::Length(1),  // hint
        ])
        .split(f.area());

    f.render_widget(
        Paragraph::new(" Welcome to coding-agent — first-run setup")
            .style(Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
        chunks[0],
    );

    let body: Vec<Line> = match w.step {
        Step::Provider => {
            let mut lines = vec![Line::from("Choose a provider (↑↓, Enter):"), Line::from("")];
            for (i, (_, label, _, _)) in PROVIDERS.iter().enumerate() {
                let marker = if i == w.provider_idx { "▶ " } else { "  " };
                let style = if i == w.provider_idx {
                    Style::default().fg(Color::Green).add_modifier(Modifier::BOLD)
                } else {
                    Style::default()
                };
                lines.push(Line::from(Span::styled(format!("{marker}{label}"), style)));
            }
            lines
        }
        Step::BaseUrl => vec![
            Line::from("Base URL for your gateway:"),
            Line::from(""),
            Line::from(format!("  {}▌", w.base_url.text())),
        ],
        Step::Key => vec![
            Line::from("API key (required):"),
            Line::from(format!("  ({})", PROVIDERS[w.provider_idx].3)),
            Line::from(""),
            Line::from(format!("  {}▌", "•".repeat(w.key.text().chars().count()))),
            Line::from(""),
            Line::from(if w.key.text().trim().is_empty() {
                "  (type your key, then Enter)"
            } else {
                "  Enter to continue"
            }),
        ],
        Step::Model => {
            let def = PROVIDERS[w.provider_idx].2;
            vec![
                Line::from(format!("Model (default: {def}):")),
                Line::from(""),
                Line::from(format!("  {}▌", w.model.text())),
            ]
        }
        Step::AutoApprove => vec![
            Line::from("Auto-approve tool actions without asking? (Space toggles, Enter confirms)"),
            Line::from(""),
            Line::from(format!("  [{}] auto-approve", if w.auto_approve { "x" } else { " " })),
        ],
        Step::Done => vec![Line::from("Saving…")],
    };
    f.render_widget(
        Paragraph::new(body)
            .block(Block::default().borders(Borders::ALL).title("setup"))
            .wrap(Wrap { trim: false }),
        chunks[1],
    );

    f.render_widget(
        Paragraph::new(" Enter: next · Ctrl-C: quit")
            .style(Style::default().fg(Color::DarkGray)),
        chunks[2],
    );
}

fn render(f: &mut Frame, state: &AppState, composer: &Composer, turn_running: bool) {
    // Input box grows with the number of lines (2 borders + content), capped.
    let input_lines = composer.line_count().clamp(1, 8) as u16;
    // Plan panel height: 0 when no plan, else steps + title (capped at 8 rows).
    let plan_h: u16 = if state.plan.is_empty() {
        0
    } else {
        ((state.plan.len() + 1).min(8) as u16) + 2 // +2 borders
    };
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),                 // header
            Constraint::Min(3),                     // transcript
            Constraint::Length(plan_h),             // live plan panel (0 = hidden)
            Constraint::Length(input_lines + 2),    // input box (+borders)
            Constraint::Length(1),                  // status / completion hint
        ])
        .split(f.area());

    // Header: name · model · context usage · turn
    let model = if state.model.is_empty() { "(no model)" } else { &state.model };
    let ctx = if state.max_context > 0 {
        format!("  ·  {}/{} ctx",
            crate::render::fmt_tokens(state.used_tokens),
            crate::render::fmt_tokens(state.max_context))
    } else if state.used_tokens > 0 {
        format!("  ·  {} ctx", crate::render::fmt_tokens(state.used_tokens))
    } else {
        String::new()
    };
    let header = format!(" coding-agent  ·  {model}{ctx}  ·  turn {}", state.turn);
    f.render_widget(
        Paragraph::new(header).style(Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
        chunks[0],
    );

    // Transcript (honor scroll offset; 0 = follow tail). Pre-wrap to visual
    // lines so slicing/tail-follow counts what's actually on screen — letting
    // Paragraph re-wrap after slicing pushed the newest lines out of view.
    let inner_w = chunks[1].width.saturating_sub(2); // minus borders
    let lines = crate::render::wrap_lines(
        state.render_lines(turn_running, inner_w), inner_w);
    let height = chunks[1].height.saturating_sub(2) as usize; // minus borders
    let start = state.view_start(lines.len(), height);
    let end = (start + height).min(lines.len());
    let visible: Vec<Line> = lines[start..end].to_vec();
    let conv_title = if state.scroll > 0 {
        format!("conversation (scrolled ↑{} — PageDn to follow)", state.scroll)
    } else {
        "conversation".to_string()
    };
    f.render_widget(
        Paragraph::new(visible)
            .block(Block::default().borders(Borders::ALL).title(conv_title)),
        chunks[1],
    );

    // Live plan/todo panel (chunks[2]) — only when a plan exists.
    if !state.plan.is_empty() {
        let plan_lines = crate::render::render_plan_panel(&state.plan);
        f.render_widget(
            Paragraph::new(plan_lines)
                .block(Block::default().borders(Borders::ALL).title("plan"))
                .wrap(Wrap { trim: false }),
            chunks[2],
        );
    }

    // Input box (multi-line aware). Empty + idle → dim-italic placeholder.
    let prompt = if turn_running { "(running — Esc to stop) " } else { "› " };
    let input_para = if composer.text().is_empty() && !turn_running {
        Paragraph::new(Line::from(vec![
            Span::raw(prompt),
            Span::styled("Ask coding-agent to do anything",
                Style::default().fg(Color::DarkGray).add_modifier(Modifier::ITALIC)),
        ]))
    } else {
        Paragraph::new(format!("{prompt}{}", composer.text()))
    };
    f.render_widget(
        input_para
            .block(Block::default().borders(Borders::ALL))
            .wrap(Wrap { trim: false }),
        chunks[3],
    );

    // Status / context-aware hint line.
    let at_tok = composer.at_token();
    let cands = composer.slash_candidates();
    if let Some((tool, args)) = &state.pending_permission {
        // Modal approval prompt takes over the whole line.
        let line = Line::from(vec![
            Span::styled(" 🔐 allow ", Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
            Span::styled(tool.clone(), Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
            Span::styled(format!("  {args}  "), Style::default().fg(Color::DarkGray)),
            Span::styled("[y]es  [n]o  [a]lways", Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
        ]);
        f.render_widget(Paragraph::new(line), chunks[4]);
    } else if state.pending_question.is_some() {
        let n = state.pending_question.as_ref().map(|(_, o)| o.len()).unwrap_or(0);
        let hint = if n > 0 {
            format!(" ❓ type an answer or a number 1–{n}, then ⏎")
        } else {
            " ❓ type your answer, then ⏎".to_string()
        };
        f.render_widget(
            Paragraph::new(hint).style(Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)),
            chunks[4],
        );
    } else if let Some(tok) = at_tok {
        // @file completion popup: show matching workspace files.
        let hits = crate::file_index::fuzzy_match(&state.files, &tok, 8);
        let text = if hits.is_empty() {
            format!(" @{tok}  (no matching files)")
        } else {
            format!(" @ ⇥  {}", hits.join("  "))
        };
        f.render_widget(
            Paragraph::new(text).style(Style::default().fg(Color::Cyan)),
            chunks[4],
        );
    } else if !cands.is_empty() {
        // While typing a slash command, show matching candidates (even one) so
        // the user can see what `/` offers and Tab-complete.
        let shown: Vec<&str> = cands.iter().take(12).copied().collect();
        let more = if cands.len() > 12 { " …" } else { "" };
        f.render_widget(
            Paragraph::new(format!(" Tab ⇥  {}{}", shown.join("  "), more))
                .style(Style::default().fg(Color::Cyan)),
            chunks[4],
        );
    } else if turn_running {
        // Codex-style busy row: "⠹ <label>  {elapsed}  esc to interrupt".
        // Label shimmers; when a tool is running it names the tool.
        let label = if state.status_str.starts_with("running ") {
            // "running shell_exec…" → "Running shell_exec"
            let t = state.status_str.trim_end_matches('…');
            let mut c = t.chars();
            c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str())
                .unwrap_or_else(|| "Working".into())
        } else {
            "Working".to_string()
        };
        let mut spans = vec![
            Span::styled(format!(" {} ", crate::render::spinner_frame(state.tick)),
                         Style::default().fg(Color::Cyan)),
        ];
        spans.extend(crate::render::shimmer_spans(&label, state.tick));
        spans.push(Span::styled(
            format!("  {}  ", crate::render::fmt_elapsed_compact(state.elapsed_secs())),
            Style::default().fg(Color::DarkGray)));
        spans.push(Span::styled("esc to interrupt", Style::default().fg(Color::DarkGray)));
        f.render_widget(Paragraph::new(Line::from(spans)), chunks[4]);
    } else {
        // Idle: mode + status + context-aware key hints.
        let mut spans = Vec::new();
        if state.auto_approve {
            // Claude Code-style mode chip when tools run unprompted.
            spans.push(Span::styled(" ⏵⏵ auto-accept ",
                Style::default().fg(Color::Black).bg(Color::Yellow).add_modifier(Modifier::BOLD)));
        }
        spans.push(Span::styled(format!(" [{}] ", state.status_str),
                                Style::default().fg(Color::DarkGray)));
        let hint = if state.scroll > 0 {
            "PgUp/PgDn scroll · End follow · ⏎ send"
        } else {
            "⏎ send · ⇧⏎ newline · / commands · @ files · ⇧⇥ auto-accept · ⌃C quit"
        };
        spans.push(Span::styled(hint, Style::default().fg(Color::DarkGray)));
        f.render_widget(Paragraph::new(Line::from(spans)), chunks[4]);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto::Event;

    fn ev(json: &str) -> Event {
        Event::from_line(json).unwrap()
    }

    /// Flatten a rendered Line into its plain text (spans concatenated).
    fn line_text(l: &Line) -> String {
        l.spans.iter().map(|s| s.content.as_ref()).collect()
    }

    /// All rendered transcript lines flattened to strings.
    fn rendered(s: &AppState) -> Vec<String> {
        s.render_lines(false, 80).iter().map(line_text).collect()
    }

    #[test]
    fn debug_log_path_interprets_env() {
        // Save/restore the env var so we don't leak into other tests.
        let prev = std::env::var("CODING_AGENT_DEBUG").ok();

        std::env::remove_var("CODING_AGENT_DEBUG");
        assert!(debug_log_path().is_none(), "unset -> disabled");

        std::env::set_var("CODING_AGENT_DEBUG", "0");
        assert!(debug_log_path().is_none(), "0 -> disabled");

        std::env::set_var("CODING_AGENT_DEBUG", "1");
        let p = debug_log_path().expect("1 -> default path");
        assert!(p.ends_with("tui.log"), "1 -> state dir tui.log, got {p:?}");

        std::env::set_var("CODING_AGENT_DEBUG", "/tmp/custom-xyz.log");
        assert_eq!(
            debug_log_path().unwrap(),
            std::path::PathBuf::from("/tmp/custom-xyz.log"),
            "explicit path preserved"
        );

        match prev {
            Some(v) => std::env::set_var("CODING_AGENT_DEBUG", v),
            None => std::env::remove_var("CODING_AGENT_DEBUG"),
        }
    }

    #[test]
    fn stream_text_accumulates_then_flushes_on_done() {
        let mut s = AppState::new();
        assert!(!s.apply(&ev("{\"type\":\"stream_text\",\"text\":\"Hel\"}")));
        assert!(!s.apply(&ev("{\"type\":\"stream_text\",\"text\":\"lo\"}")));
        assert_eq!(s.live, "Hello");
        let ended = s.apply(&ev("{\"type\":\"done\",\"turns\":1}"));
        assert!(ended);
        assert!(s.live.is_empty());
        assert!(rendered(&s).iter().any(|l| l.contains("Hello")));
    }

    #[test]
    fn thinking_sets_status_and_clears_error() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"error\",\"error\":\"boom\"}"));
        assert_eq!(s.last_error.as_deref(), Some("boom"));
        s.apply(&ev("{\"type\":\"thinking\",\"turn\":2}"));
        assert_eq!(s.turn, 2);
        assert!(s.last_error.is_none());
    }

    #[test]
    fn tool_call_and_result_render() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"tool_call\",\"name\":\"grep\"}"));
        s.apply(&ev("{\"type\":\"tool_result\",\"is_error\":false}"));
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("grep"));
        assert!(joined.contains("✅"));
    }

    #[test]
    fn tool_call_surfaces_path_target() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"tool_call\",\"name\":\"file_edit\",\"arguments\":{\"path\":\"src/main.py\"}}"));
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("file_edit"));
        assert!(joined.contains("src/main.py"));
    }

    #[test]
    fn tool_call_sets_named_running_status() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"tool_call\",\"name\":\"shell_exec\",\"arguments\":{}}"));
        // status names the active tool, not a generic "running tool…"
        assert_eq!(s.status_str, "running shell_exec…");
    }

    #[test]
    fn tool_result_diff_body_is_rendered() {
        let mut s = AppState::new();
        // result carrying a unified diff → shown with +/- lines
        s.apply(&ev("{\"type\":\"tool_result\",\"is_error\":false,\"result\":\"@@ -1 +1 @@\\n-old\\n+new\"}"));
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("-old"));
        assert!(joined.contains("+new"));
    }

    #[test]
    fn summarize_caps_plain_text() {
        let body = "l1\nl2\nl3\nl4\nl5";
        let s = summarize_result_body(body);
        assert!(s.contains("l1") && s.contains("l3"));
        assert!(!s.contains("l5"));
        assert!(s.contains("…"));
    }

    #[test]
    fn summarize_keeps_diff_lines() {
        let body = "@@ -1 +1 @@\n-a\n+b";
        let s = summarize_result_body(body);
        assert!(s.contains("-a") && s.contains("+b"));
    }

    #[test]
    fn error_event_records_and_renders() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"error\",\"error\":\"bad thing\"}"));
        assert!(rendered(&s).iter().any(|l| l.contains("bad thing")));
    }

    #[test]
    fn push_user_and_live_cursor() {
        let mut s = AppState::new();
        s.push_user("hi there");
        s.apply(&ev("{\"type\":\"stream_text\",\"text\":\"working\"}"));
        let lines = rendered(&s);
        assert!(lines.iter().any(|l| l.contains("hi there")));
        assert!(lines.iter().any(|l| l.contains("working")));
    }

    #[test]
    fn ready_sets_model() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"ready\",\"model\":\"claude-opus-4-8\"}"));
        assert_eq!(s.model, "claude-opus-4-8");
    }

    #[test]
    fn ready_captures_max_context() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"ready\",\"model\":\"m\",\"max_context_tokens\":200000}"));
        assert_eq!(s.max_context, 200000);
    }

    #[test]
    fn ready_and_config_updated_track_auto_approve() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"ready\",\"model\":\"m\",\"auto_approve\":true}"));
        assert!(s.auto_approve);
        s.apply(&ev("{\"type\":\"config_updated\",\"auto_approve\":false}"));
        assert!(!s.auto_approve);
    }

    #[test]
    fn model_changed_updates_header_model() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"model_changed\",\"model\":\"gpt-4o\"}"));
        assert_eq!(s.model, "gpt-4o");
    }

    #[test]
    fn command_result_shows_as_notice() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"command_result\",\"text\":\"Current model: claude\"}"));
        assert!(rendered(&s).iter().any(|l| l.contains("Current model: claude")));
    }

    #[test]
    fn plan_event_populates_panel() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"plan\",\"steps\":[{\"step\":\"read code\",\"status\":\"completed\"},{\"step\":\"fix bug\",\"status\":\"in_progress\"}]}"));
        assert_eq!(s.plan.len(), 2);
        assert_eq!(s.plan[0], ("read code".to_string(), "completed".to_string()));
        assert_eq!(s.plan[1].1, "in_progress");
    }

    #[test]
    fn shell_output_renders_and_ends_turn() {
        let mut s = AppState::new();
        let ended = s.apply(&ev("{\"type\":\"shell_output\",\"command\":\"ls\",\"output\":\"a.rs\\nb.rs\"}"));
        assert!(ended, "shell passthrough ends the turn");
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("ls"));
        assert!(joined.contains("a.rs"));
    }

    #[test]
    fn permission_request_sets_modal_state() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"permission_request\",\"tool_name\":\"shell_exec\",\"arguments\":{\"command\":\"rm -rf /tmp/x\"}}"));
        let (tool, args) = s.pending_permission.as_ref().expect("modal set");
        assert_eq!(tool, "shell_exec");
        // salient arg shown directly, not wrapped in JSON braces
        assert_eq!(args, "rm -rf /tmp/x");
        assert_eq!(s.status_str, "awaiting approval");
    }

    #[test]
    fn permission_request_truncates_long_args() {
        let mut s = AppState::new();
        let long = "x".repeat(500);
        s.apply(&ev(&format!(
            "{{\"type\":\"permission_request\",\"tool_name\":\"t\",\"arguments\":{{\"a\":\"{long}\"}}}}")));
        let (_, args) = s.pending_permission.as_ref().unwrap();
        assert!(args.chars().count() <= 170, "args summary is capped");
    }

    #[test]
    fn edit_permission_shows_diff_preview() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"permission_request\",\"tool_name\":\"file_edit\",\"arguments\":{\"path\":\"a.py\",\"old_text\":\"x = 1\",\"new_text\":\"x = 2\"}}"));
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("-x = 1"), "old line shown as deletion: {joined}");
        assert!(joined.contains("+x = 2"), "new line shown as addition");
        assert!(s.pending_permission.is_some());
    }

    #[test]
    fn write_permission_shows_content_preview() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"permission_request\",\"tool_name\":\"file_write\",\"arguments\":{\"path\":\"b.py\",\"content\":\"print(1)\"}}"));
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("+print(1)"));
    }

    #[test]
    fn shell_permission_has_no_diff_preview() {
        let mut s = AppState::new();
        let before = s.transcript.len();
        s.apply(&ev("{\"type\":\"permission_request\",\"tool_name\":\"shell_exec\",\"arguments\":{\"command\":\"ls\"}}"));
        assert_eq!(s.transcript.len(), before, "non-edit tools add no preview");
    }

    #[test]
    fn sessions_list_adopts_latest_when_continuing() {
        let mut s = AppState::new();
        s.want_continue = true;
        s.apply(&ev("{\"type\":\"sessions_list\",\"sessions\":[{\"id\":\"abc12345678\",\"metadata\":{\"title\":\"fix the bug\"}},{\"id\":\"older\"}]}"));
        assert_eq!(s.session_id.as_deref(), Some("abc12345678"));
        assert!(!s.want_continue, "one-shot");
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("Resumed session abc12345"));
        assert!(joined.contains("fix the bug"));
    }

    #[test]
    fn sessions_list_ignored_when_not_continuing() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"sessions_list\",\"sessions\":[{\"id\":\"abc\"}]}"));
        assert!(s.session_id.is_none());
    }

    #[test]
    fn session_state_tracks_session_id() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"session_state\",\"session_id\":\"sess-9\",\"turn_count\":1}"));
        assert_eq!(s.session_id.as_deref(), Some("sess-9"));
    }

    #[test]
    fn question_event_sets_modal_and_renders_options() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"question\",\"question\":\"Which DB?\",\"options\":[\"sqlite\",\"postgres\"]}"));
        let (q, opts) = s.pending_question.as_ref().expect("question modal set");
        assert_eq!(q, "Which DB?");
        assert_eq!(opts.len(), 2);
        assert_eq!(s.status_str, "awaiting answer");
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("Which DB?"));
        assert!(joined.contains("1. sqlite"));
        assert!(joined.contains("2. postgres"));
    }

    #[test]
    fn resume_opens_picker_with_labels() {
        let mut s = AppState::new();
        s.want_resume = true;
        s.apply(&ev("{\"type\":\"sessions_list\",\"sessions\":[{\"id\":\"abc12345678\",\"updated_at\":\"2026-07-02\",\"metadata\":{\"title\":\"fix bug\"}},{\"id\":\"def99999999\",\"metadata\":{}}]}"));
        let (items, sel) = s.session_picker.as_ref().expect("picker open");
        assert_eq!(items.len(), 2);
        assert_eq!(*sel, 0);
        assert!(items[0].1.contains("abc12345"));
        assert!(items[0].1.contains("fix bug"));
        assert!(items[1].1.contains("(untitled)"));
        // picker rendered in the transcript lines
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("Resume which session?"));
        assert!(joined.contains("▶"));
    }

    #[test]
    fn resume_with_no_sessions_starts_fresh() {
        let mut s = AppState::new();
        s.want_resume = true;
        s.apply(&ev("{\"type\":\"sessions_list\",\"sessions\":[]}"));
        assert!(s.session_picker.is_none());
        assert!(rendered(&s).join("\n").contains("starting fresh"));
    }

    #[test]
    fn session_state_updates_context_usage() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"session_state\",\"prompt_tokens\":1000,\"completion_tokens\":500,\"max_context_tokens\":200000}"));
        assert_eq!(s.used_tokens, 1500);
        assert_eq!(s.max_context, 200000);
    }

    #[test]
    fn rewound_event_truncates_transcript_and_stages_text() {
        let mut s = AppState::new();
        s.push_user("first ask");
        s.transcript.push(crate::render::Entry::Agent("answer".into()));
        s.turn = 1;
        s.apply(&ev("{\"type\":\"rewound\",\"text\":\"first ask\"}"));
        // user+agent entries removed, notice added, text staged
        assert!(s.transcript.iter().all(|e| !matches!(e, crate::render::Entry::Agent(_))));
        assert_eq!(s.rewound_text.as_deref(), Some("first ask"));
        assert_eq!(s.turn, 0);
        assert!(rendered(&s).join("\n").contains("Rewound"));
    }

    #[test]
    fn rewound_empty_text_is_noop() {
        let mut s = AppState::new();
        let before = s.transcript.len();
        s.apply(&ev("{\"type\":\"rewound\",\"text\":\"\"}"));
        assert_eq!(s.transcript.len(), before);
        assert!(s.rewound_text.is_none());
    }

    #[test]
    fn turn_end_appends_summary_line() {
        let mut s = AppState::new();
        s.turn = 2;
        s.apply(&ev("{\"type\":\"session_state\",\"prompt_tokens\":12000,\"completion_tokens\":300,\"max_context_tokens\":200000}"));
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("✓ turn 2"), "summary shows turn number: {joined}");
        assert!(joined.contains("12.3k/200.0k ctx"), "summary shows ctx usage");
    }

    #[test]
    fn no_summary_without_tokens() {
        let mut s = AppState::new();
        let before = s.transcript.len();
        // session_state without token fields (e.g. --continue adoption) → no noise
        s.apply(&ev("{\"type\":\"session_state\",\"session_id\":\"x\",\"turn_count\":0}"));
        assert_eq!(s.transcript.len(), before);
    }

    #[test]
    fn summary_includes_cost_when_present() {
        let mut s = AppState::new();
        s.turn = 1;
        s.apply(&ev("{\"type\":\"session_state\",\"prompt_tokens\":1000,\"completion_tokens\":100,\"max_context_tokens\":200000,\"cost_usd\":0.0234}"));
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("$0.0234"), "cost shown when backend prices it: {joined}");
    }

    #[test]
    fn new_state_shows_welcome_notice() {
        let s = AppState::new();
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("Welcome"), "entry screen should greet the user");
        assert!(joined.to_lowercase().contains("command"),
                "welcome should mention slash commands");
    }

    #[test]
    fn empty_screen_shows_suggestions_then_hides_after_message() {
        let mut s = AppState::new();
        // Fresh: suggestions visible.
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("Explain this codebase"), "empty screen shows suggestions");
        assert!(joined.contains("Try:"));
        // After a user message, suggestions disappear.
        s.push_user("do the thing");
        let joined2 = rendered(&s).join("\n");
        assert!(!joined2.contains("Explain this codebase"),
                "suggestions hidden once a conversation starts");
    }

    #[test]
    fn elapsed_secs_from_ticks() {
        let mut s = AppState::new();
        s.busy_start_tick = 5;
        s.tick = 35; // 30 ticks @ 10Hz = 3s
        assert_eq!(s.elapsed_secs(), 3);
    }

    #[test]
    fn reasoning_streams_then_clears_when_answer_starts() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"stream_reasoning\",\"text\":\"let me think\"}"));
        let shown = s.render_lines(true, 80).iter()
            .map(|l| l.spans.iter().map(|sp| sp.content.as_ref()).collect::<String>())
            .any(|t| t.contains("let me think"));
        assert!(shown, "reasoning should be displayed while streaming");
        // Once answer text starts, reasoning is cleared.
        s.apply(&ev("{\"type\":\"stream_text\",\"text\":\"the answer\"}"));
        assert!(s.live_reasoning.is_empty(), "reasoning cleared once answer starts");
        let after = s.render_lines(true, 80).iter()
            .map(|l| l.spans.iter().map(|sp| sp.content.as_ref()).collect::<String>())
            .collect::<Vec<_>>().join("\n");
        assert!(after.contains("the answer"));
        assert!(!after.contains("let me think"));
    }

    #[test]
    fn busy_shows_thinking_indicator_before_text() {
        let mut s = AppState::new();
        // Turn started (thinking) but nothing streamed yet.
        s.apply(&ev("{\"type\":\"thinking\",\"turn\":1}"));
        let busy = s.render_lines(true, 80).iter()
            .map(|l| l.spans.iter().map(|sp| sp.content.as_ref()).collect::<String>())
            .any(|t| t.contains("thinking"));
        assert!(busy, "a thinking indicator should show while busy with no live text");
        // Once text streams, the indicator gives way to the live text.
        s.apply(&ev("{\"type\":\"stream_text\",\"text\":\"hello\"}"));
        let has_text = s.render_lines(true, 80).iter()
            .map(|l| l.spans.iter().map(|sp| sp.content.as_ref()).collect::<String>())
            .any(|t| t.contains("hello"));
        assert!(has_text);
    }

    #[test]
    fn view_start_follows_tail_by_default() {
        let s = AppState::new();
        // 100 lines, 10 visible, no scroll → start at 90
        assert_eq!(s.view_start(100, 10), 90);
        // fewer lines than view → start at 0
        assert_eq!(s.view_start(5, 10), 0);
    }

    #[test]
    fn scroll_up_clamps_and_offsets_view() {
        let mut s = AppState::new();
        s.scroll_up(5, 100, 10);
        assert_eq!(s.scroll, 5);
        assert_eq!(s.view_start(100, 10), 85); // 90 tail - 5 scroll
        // clamp: can't scroll past the top (max = 100-10 = 90)
        s.scroll_up(1000, 100, 10);
        assert_eq!(s.scroll, 90);
        assert_eq!(s.view_start(100, 10), 0);
    }

    #[test]
    fn scroll_down_returns_to_tail() {
        let mut s = AppState::new();
        s.scroll_up(20, 100, 10);
        s.scroll_down(5);
        assert_eq!(s.scroll, 15);
        s.scroll_down(1000); // clamps at 0 (tail)
        assert_eq!(s.scroll, 0);
    }
}
