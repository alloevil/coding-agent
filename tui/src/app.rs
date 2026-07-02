//! Full-screen Ratatui app — Phase 2.
//!
//! Layout: scrollable transcript (top) + bordered input box (bottom) + a
//! one-line status. Pure state (`AppState`) is separated from IO so the
//! event->state mapping is unit-testable; the ratatui render + crossterm
//! keyboard loop live in `run`.

use std::io::Stdout;

use crossterm::event::{DisableBracketedPaste, EnableBracketedPaste, Event as CtEvent, KeyCode,
                       KeyEventKind};
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
/// - "1" / "true" / "on"    → default file in the state dir
///                            ($XDG_STATE_HOME|~/.local/state)/coding-agent/tui.log
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

/// Whether the agent is idle (accepting input) or busy (running a turn).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
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
}

impl AppState {
    pub fn new() -> Self {
        let mut s = AppState::default();
        s.status_str = "ready".into();
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
            }
            "stream_text" => {
                if let Some(t) = ev.str_field("text") {
                    self.live.push_str(t);
                }
                self.status_str = Status::Thinking.label().into();
            }
            "assistant_message" => {
                // Prefer streamed text; fall back to the full content.
                let final_text = if !self.live.is_empty() {
                    std::mem::take(&mut self.live)
                } else {
                    ev.str_field("content").unwrap_or("").to_string()
                };
                if !final_text.is_empty() {
                    self.transcript.push(Entry::Agent(final_text));
                }
            }
            "tool_call" => {
                self.status_str = Status::RunningTool.label().into();
                let name = ev.str_field("name").unwrap_or("?").to_string();
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
    pub fn render_lines(&self, busy: bool) -> Vec<Line<'static>> {
        use crate::render::{render_entry, render_markdown, spinner_frame};
        let mut out: Vec<Line<'static>> = Vec::new();
        for e in &self.transcript {
            out.extend(render_entry(e));
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
        } else if busy {
            // Thinking: model is working but hasn't streamed text yet. Show an
            // animated indicator in the transcript so the screen isn't static.
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
        out
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
pub async fn run(mut backend: Backend, model_hint: String, force_setup: bool) -> std::io::Result<()> {
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
    execute!(stdout, EnterAlternateScreen, EnableBracketedPaste)?;
    let mut term = Terminal::new(CrosstermBackend::new(stdout))?;

    let mut state = AppState::new();
    state.model = model_hint;
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
    execute!(term.backend_mut(), LeaveAlternateScreen, DisableBracketedPaste).ok();
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
        CtEvent::Key(k) if k.kind != KeyEventKind::Release => {
            use crossterm::event::KeyModifiers as M;
            match (k.code, k.modifiers) {
                (KeyCode::Char('c'), M::CONTROL) => state.should_quit = true,
                (KeyCode::Esc, _) => {
                    // Interrupt a running turn (relies on concurrent protocol).
                    if *turn_running {
                        backend.send(&Request::Interrupt).await?;
                    }
                }
                (KeyCode::Enter, m) if m.contains(M::SHIFT) || m.contains(M::ALT) => {
                    composer.newline(); // multi-line input
                }
                (KeyCode::Enter, _) => {
                    if !*turn_running {
                        let text = composer.take();
                        if !text.trim().is_empty() {
                            state.push_user(&text);
                            state.scroll = 0; // follow tail on new input
                            backend.send(&Request::UserInput { content: text, session_id: None }).await?;
                            *turn_running = true;
                        }
                    }
                }
                (KeyCode::Tab, _) => { composer.complete_slash(); }
                (KeyCode::Up, _) => composer.history_prev(),
                (KeyCode::Down, _) => composer.history_next(),
                (KeyCode::PageUp, _) => {
                    let total = state.render_lines(*turn_running).len();
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
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),                 // header
            Constraint::Min(3),                     // transcript
            Constraint::Length(input_lines + 2),    // input box (+borders)
            Constraint::Length(1),                  // status / completion hint
        ])
        .split(f.area());

    // Header
    let header = format!(" coding-agent  ·  {}  ·  turn {}", state.model, state.turn);
    f.render_widget(
        Paragraph::new(header).style(Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
        chunks[0],
    );

    // Transcript (honor scroll offset; 0 = follow tail)
    let lines = state.render_lines(turn_running);
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
            .block(Block::default().borders(Borders::ALL).title(conv_title))
            .wrap(Wrap { trim: false }),
        chunks[1],
    );

    // Input box (multi-line aware)
    let prompt = if turn_running { "(running — Esc to stop) " } else { "› " };
    let input_text = format!("{prompt}{}", composer.text());
    f.render_widget(
        Paragraph::new(input_text)
            .block(Block::default().borders(Borders::ALL))
            .wrap(Wrap { trim: false }),
        chunks[2],
    );

    // Status / context-aware hint line.
    let cands = composer.slash_candidates();
    if !cands.is_empty() {
        // While typing a slash command, show matching candidates (even one) so
        // the user can see what `/` offers and Tab-complete.
        let shown: Vec<&str> = cands.iter().take(12).copied().collect();
        let more = if cands.len() > 12 { " …" } else { "" };
        f.render_widget(
            Paragraph::new(format!(" Tab ⇥  {}{}", shown.join("  "), more))
                .style(Style::default().fg(Color::Cyan)),
            chunks[3],
        );
    } else {
        // Left: status (with spinner when busy). Right: context-aware key hints.
        let busy = turn_running;
        let status_span = if busy {
            Span::styled(
                format!(" {} {} ", crate::render::spinner_frame(state.tick), state.status_str),
                Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD),
            )
        } else {
            Span::styled(format!(" [{}] ", state.status_str),
                         Style::default().fg(Color::DarkGray))
        };
        let hint = if state.scroll > 0 {
            "PgUp/PgDn scroll · End follow · ⏎ send"
        } else if busy {
            "esc stop · ⌃C quit"
        } else {
            "⏎ send · ⇧⏎ newline · / commands · ↑↓ history · ⌃C quit"
        };
        let line = Line::from(vec![
            status_span,
            Span::styled(hint, Style::default().fg(Color::DarkGray)),
        ]);
        f.render_widget(Paragraph::new(line), chunks[3]);
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
        s.render_lines(false).iter().map(line_text).collect()
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
    fn new_state_shows_welcome_notice() {
        let s = AppState::new();
        let joined = rendered(&s).join("\n");
        assert!(joined.contains("Welcome"), "entry screen should greet the user");
        assert!(joined.to_lowercase().contains("command"),
                "welcome should mention slash commands");
    }

    #[test]
    fn busy_shows_thinking_indicator_before_text() {
        let mut s = AppState::new();
        // Turn started (thinking) but nothing streamed yet.
        s.apply(&ev("{\"type\":\"thinking\",\"turn\":1}"));
        let busy = s.render_lines(true).iter()
            .map(|l| l.spans.iter().map(|sp| sp.content.as_ref()).collect::<String>())
            .any(|t| t.contains("thinking"));
        assert!(busy, "a thinking indicator should show while busy with no live text");
        // Once text streams, the indicator gives way to the live text.
        s.apply(&ev("{\"type\":\"stream_text\",\"text\":\"hello\"}"));
        let has_text = s.render_lines(true).iter()
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
