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
    /// Committed transcript lines (already-finalized user/agent/tool text).
    pub transcript: Vec<String>,
    /// In-progress streaming assistant text (flushed to transcript on done).
    pub live: String,
    pub status_str: String,
    pub model: String,
    pub turn: u64,
    pub last_error: Option<String>,
    pub should_quit: bool,
    /// Scroll offset from the bottom (0 = follow tail; N = scrolled up N lines).
    pub scroll: usize,
}

impl AppState {
    pub fn new() -> Self {
        let mut s = AppState::default();
        s.status_str = "ready".into();
        s
    }

    pub fn push_user(&mut self, text: &str) {
        self.transcript.push(format!("You: {text}"));
    }

    /// Apply one protocol event to the state. Returns true if the turn ended.
    pub fn apply(&mut self, ev: &Event) -> bool {
        match ev.kind.as_str() {
            "ready" => {
                self.model = ev.str_field("model").unwrap_or("").to_string();
                self.status_str = Status::Idle.label().into();
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
                    self.transcript.push(format!("Agent: {final_text}"));
                }
            }
            "tool_call" => {
                self.status_str = Status::RunningTool.label().into();
                let name = ev.str_field("name").unwrap_or("?");
                self.transcript.push(format!("  🔧 {name}"));
            }
            "tool_result" => {
                let is_err = ev.rest.get("is_error").and_then(|v| v.as_bool()).unwrap_or(false);
                let mark = if is_err { "❌" } else { "✅" };
                self.transcript.push(format!("  {mark}"));
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
                    self.transcript.push(format!("Agent: {t}"));
                }
                self.status_str = Status::Idle.label().into();
                return true;
            }
            _ => {}
        }
        false
    }

    /// Lines to render in the transcript (committed + in-progress live).
    pub fn render_lines(&self) -> Vec<String> {
        let mut out = self.transcript.clone();
        if !self.live.is_empty() {
            out.push(format!("Agent: {}▌", self.live));
        }
        if let Some(e) = &self.last_error {
            out.push(format!("❌ {e}"));
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

/// Run the full-screen TUI event loop. Drives the backend and renders state.
pub async fn run(mut backend: Backend, model_hint: String) -> std::io::Result<()> {
    enable_raw_mode()?;
    let mut stdout = std::io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableBracketedPaste)?;
    let mut term = Terminal::new(CrosstermBackend::new(stdout))?;

    let mut state = AppState::new();
    state.model = model_hint;
    let mut composer = Composer::new();
    let mut turn_running = false;

    let mut keys = crossterm::event::EventStream::new();

    let result = run_loop(&mut term, &mut backend, &mut state, &mut composer,
                          &mut turn_running, &mut keys).await;

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
    keys: &mut crossterm::event::EventStream,
) -> std::io::Result<()> {
    loop {
        term.draw(|f| render(f, state, composer, *turn_running))?;
        if state.should_quit {
            return Ok(());
        }

        tokio::select! {
            // Backend event
            maybe_ev = backend.events.recv() => {
                match maybe_ev {
                    Some(ev) => {
                        let ended = state.apply(&ev);
                        if ended {
                            *turn_running = false;
                        }
                    }
                    None => return Ok(()), // backend closed
                }
            }
            // Keyboard event
            maybe_key = keys.next() => {
                if let Some(Ok(ct)) = maybe_key {
                    handle_key(ct, backend, state, composer, turn_running).await?;
                }
            }
        }
    }
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
                    let total = state.render_lines().len();
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
    let lines = state.render_lines();
    let height = chunks[1].height.saturating_sub(2) as usize; // minus borders
    let start = state.view_start(lines.len(), height);
    let end = (start + height).min(lines.len());
    let visible: Vec<Line> = lines[start..end]
        .iter()
        .map(|l| {
            if l.starts_with("You:") {
                Line::from(Span::styled(l.clone(), Style::default().fg(Color::Green)))
            } else {
                Line::from(l.clone())
            }
        })
        .collect();
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

    // Status / completion-hint line
    let cands = composer.slash_candidates();
    let status = if cands.len() > 1 {
        format!(" Tab: {}", cands.join(" "))
    } else {
        let hint = if turn_running { "Esc stop · Ctrl-C quit" }
                   else { "↑↓ history · Tab complete · Shift+Enter newline · PgUp/PgDn scroll" };
        format!(" [{}]  {hint}", state.status_str)
    };
    f.render_widget(
        Paragraph::new(status).style(Style::default().fg(Color::DarkGray)),
        chunks[3],
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto::Event;

    fn ev(json: &str) -> Event {
        Event::from_line(json).unwrap()
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
        assert!(s.transcript.iter().any(|l| l == "Agent: Hello"));
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
        let joined = s.transcript.join("\n");
        assert!(joined.contains("grep"));
        assert!(joined.contains("✅"));
    }

    #[test]
    fn error_event_records_and_renders() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"error\",\"error\":\"bad thing\"}"));
        assert!(s.render_lines().iter().any(|l| l.contains("bad thing")));
    }

    #[test]
    fn push_user_and_live_cursor() {
        let mut s = AppState::new();
        s.push_user("hi there");
        s.apply(&ev("{\"type\":\"stream_text\",\"text\":\"working\"}"));
        let lines = s.render_lines();
        assert!(lines.iter().any(|l| l == "You: hi there"));
        assert!(lines.iter().any(|l| l.contains("working▌")));
    }

    #[test]
    fn ready_sets_model() {
        let mut s = AppState::new();
        s.apply(&ev("{\"type\":\"ready\",\"model\":\"claude-opus-4-8\"}"));
        assert_eq!(s.model, "claude-opus-4-8");
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
