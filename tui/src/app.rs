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
                (KeyCode::Enter, _) => {
                    if !*turn_running {
                        let text = composer.take();
                        if !text.trim().is_empty() {
                            state.push_user(&text);
                            backend.send(&Request::UserInput { content: text, session_id: None }).await?;
                            *turn_running = true;
                        }
                    }
                }
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
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),   // header
            Constraint::Min(3),      // transcript
            Constraint::Length(3),   // input box
            Constraint::Length(1),   // status
        ])
        .split(f.area());

    // Header
    let header = format!(" coding-agent  ·  {}  ·  turn {}", state.model, state.turn);
    f.render_widget(
        Paragraph::new(header).style(Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
        chunks[0],
    );

    // Transcript (show the tail that fits)
    let lines = state.render_lines();
    let height = chunks[1].height.saturating_sub(2) as usize; // minus borders
    let start = lines.len().saturating_sub(height);
    let visible: Vec<Line> = lines[start..]
        .iter()
        .map(|l| {
            if l.starts_with("You:") {
                Line::from(Span::styled(l.clone(), Style::default().fg(Color::Green)))
            } else {
                Line::from(l.clone())
            }
        })
        .collect();
    f.render_widget(
        Paragraph::new(visible)
            .block(Block::default().borders(Borders::ALL).title("conversation"))
            .wrap(Wrap { trim: false }),
        chunks[1],
    );

    // Input box
    let prompt = if turn_running { " (running — please wait) " } else { " › " };
    let input = format!("{prompt}{}", composer.text());
    f.render_widget(
        Paragraph::new(input).block(Block::default().borders(Borders::ALL)),
        chunks[2],
    );

    // Status line
    let status = format!(" [{}]  Ctrl-C quit", state.status_str);
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
}
