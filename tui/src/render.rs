//! Rich transcript rendering — typed entries → styled ratatui `Line`s.
//!
//! The transcript is a list of typed [`Entry`] items (user / agent / tool /
//! tool-result / notice) rather than pre-formatted strings, so rendering can
//! style each kind: agent text gets lightweight markdown, tool results get
//! diff coloring, roles get a colored gutter bar. All functions here are pure
//! (Entry/&str -> Vec<Line>) so they're unit-testable without a terminal.

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};

/// One logical transcript item. Kept minimal & cloneable so AppState stays
/// simple to snapshot/test.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Entry {
    /// User input.
    User(String),
    /// Finalized assistant message (rendered as markdown).
    Agent(String),
    /// A tool invocation line: (tool_name, optional target e.g. path).
    ToolCall { name: String, target: String },
    /// A tool result: ok/err + optional body (unified diff or text).
    ToolResult { ok: bool, body: String },
    /// A system notice (config saved, etc).
    Notice(String),
}

// ── palette ─────────────────────────────────────────────────────────
const C_USER: Color = Color::Green;
const C_AGENT: Color = Color::White;
const C_TOOL: Color = Color::Yellow;
const C_NOTICE: Color = Color::Cyan;
const C_ERR: Color = Color::Red;
const C_DIM: Color = Color::DarkGray;
const C_ADD: Color = Color::Green;
const C_DEL: Color = Color::Red;
const C_HUNK: Color = Color::Cyan;
const C_CODE_BG: Color = Color::Rgb(40, 40, 40);

fn gutter(color: Color) -> Span<'static> {
    Span::styled("▌", Style::default().fg(color))
}

/// Render one entry to one-or-more styled lines.
pub fn render_entry(e: &Entry) -> Vec<Line<'static>> {
    match e {
        Entry::User(t) => vec![Line::from(vec![
            gutter(C_USER),
            Span::styled("You  ", Style::default().fg(C_USER).add_modifier(Modifier::BOLD)),
            Span::raw(t.clone()),
        ])],
        Entry::Agent(t) => {
            let mut lines = vec![Line::from(vec![
                gutter(C_AGENT),
                Span::styled("Agent", Style::default().fg(C_AGENT).add_modifier(Modifier::BOLD)),
            ])];
            lines.extend(render_markdown(t));
            lines
        }
        Entry::ToolCall { name, target } => {
            let mut spans = vec![
                Span::raw("  "),
                Span::styled(format!("🔧 {name}"), Style::default().fg(C_TOOL)),
            ];
            if !target.is_empty() {
                spans.push(Span::styled(format!("  {target}"), Style::default().fg(C_DIM)));
            }
            vec![Line::from(spans)]
        }
        Entry::ToolResult { ok, body } => {
            let mark = if *ok { "✅" } else { "❌" };
            let mut lines = vec![Line::from(Span::styled(
                format!("  {mark}"),
                Style::default().fg(if *ok { C_ADD } else { C_ERR }),
            ))];
            if !body.is_empty() {
                lines.extend(render_diff_or_text(body));
            }
            lines
        }
        Entry::Notice(t) => vec![Line::from(Span::styled(
            t.clone(),
            Style::default().fg(C_NOTICE),
        ))],
    }
}

/// Lightweight markdown → styled lines. Handles: fenced code blocks (```),
/// ATX headings (#..), bullet lists (- / *), and inline `code` + **bold**.
/// Not a full parser — just the constructs an agent commonly emits.
pub fn render_markdown(text: &str) -> Vec<Line<'static>> {
    let mut out: Vec<Line<'static>> = Vec::new();
    let mut in_code = false;
    for raw in text.lines() {
        let trimmed = raw.trim_end();
        if trimmed.trim_start().starts_with("```") {
            in_code = !in_code;
            continue; // hide the fence markers themselves
        }
        if in_code {
            out.push(Line::from(Span::styled(
                format!("  {trimmed}"),
                Style::default().fg(Color::White).bg(C_CODE_BG),
            )));
            continue;
        }
        // Headings
        if let Some(h) = trimmed.strip_prefix("### ").or_else(|| trimmed.strip_prefix("## "))
            .or_else(|| trimmed.strip_prefix("# ")) {
            out.push(Line::from(Span::styled(
                h.to_string(),
                Style::default().fg(Color::Magenta).add_modifier(Modifier::BOLD),
            )));
            continue;
        }
        // Bullet lists
        let bullet = trimmed.trim_start();
        if let Some(item) = bullet.strip_prefix("- ").or_else(|| bullet.strip_prefix("* ")) {
            let mut spans = vec![Span::styled("  • ", Style::default().fg(C_DIM))];
            spans.extend(render_inline(item));
            out.push(Line::from(spans));
            continue;
        }
        out.push(Line::from(render_inline(trimmed)));
    }
    out
}

/// Inline markdown: `code` (highlighted) and **bold**. Returns styled spans.
pub fn render_inline(text: &str) -> Vec<Span<'static>> {
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut buf = String::new();
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '`' {
            if !buf.is_empty() {
                spans.push(Span::raw(std::mem::take(&mut buf)));
            }
            let mut code = String::new();
            while let Some(&n) = chars.peek() {
                chars.next();
                if n == '`' { break; }
                code.push(n);
            }
            spans.push(Span::styled(code, Style::default().fg(Color::Yellow).bg(C_CODE_BG)));
        } else if c == '*' && chars.peek() == Some(&'*') {
            chars.next(); // consume second *
            if !buf.is_empty() {
                spans.push(Span::raw(std::mem::take(&mut buf)));
            }
            let mut bold = String::new();
            while let Some(&n) = chars.peek() {
                chars.next();
                if n == '*' && chars.peek() == Some(&'*') {
                    chars.next();
                    break;
                }
                bold.push(n);
            }
            spans.push(Span::styled(bold, Style::default().add_modifier(Modifier::BOLD)));
        } else {
            buf.push(c);
        }
    }
    if !buf.is_empty() {
        spans.push(Span::raw(buf));
    }
    if spans.is_empty() {
        spans.push(Span::raw(String::new()));
    }
    spans
}

/// If `body` looks like a unified diff, color it (+ green, - red, @@ cyan);
/// otherwise render as dim plain text (indented).
pub fn render_diff_or_text(body: &str) -> Vec<Line<'static>> {
    let looks_diff = body.lines().any(|l| l.starts_with("@@") || l.starts_with("+++")
        || l.starts_with("---"));
    body.lines().map(|l| {
        let style = if !looks_diff {
            Style::default().fg(C_DIM)
        } else if l.starts_with("@@") {
            Style::default().fg(C_HUNK)
        } else if l.starts_with('+') {
            Style::default().fg(C_ADD)
        } else if l.starts_with('-') {
            Style::default().fg(C_DEL)
        } else {
            Style::default().fg(C_DIM)
        };
        Line::from(Span::styled(format!("     {l}"), style))
    }).collect()
}

/// Braille spinner frames for busy status. Index by a tick counter.
pub const SPINNER: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

pub fn spinner_frame(tick: usize) -> &'static str {
    SPINNER[tick % SPINNER.len()]
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text_of(line: &Line) -> String {
        line.spans.iter().map(|s| s.content.as_ref()).collect()
    }

    #[test]
    fn user_entry_has_gutter_and_label() {
        let ls = render_entry(&Entry::User("hi".into()));
        assert_eq!(ls.len(), 1);
        let t = text_of(&ls[0]);
        assert!(t.contains("You"));
        assert!(t.contains("hi"));
    }

    #[test]
    fn markdown_hides_code_fences_and_keeps_body() {
        let md = "before\n```\ncode line\n```\nafter";
        let ls = render_markdown(md);
        let joined: String = ls.iter().map(|l| text_of(l)).collect::<Vec<_>>().join("\n");
        assert!(joined.contains("code line"));
        assert!(!joined.contains("```"));
    }

    #[test]
    fn markdown_heading_stripped() {
        let ls = render_markdown("## Title");
        assert_eq!(text_of(&ls[0]), "Title");
    }

    #[test]
    fn inline_code_and_bold_split_into_spans() {
        let spans = render_inline("use `foo` and **bar**");
        // Should contain a styled code span "foo" and bold span "bar"
        let contents: Vec<String> = spans.iter().map(|s| s.content.to_string()).collect();
        assert!(contents.iter().any(|c| c == "foo"));
        assert!(contents.iter().any(|c| c == "bar"));
    }

    #[test]
    fn diff_lines_colored_by_prefix() {
        let diff = "@@ -1 +1 @@\n-old\n+new";
        let ls = render_diff_or_text(diff);
        assert_eq!(ls.len(), 3);
        // hunk header, deletion, addition
        assert_eq!(ls[0].spans[0].style.fg, Some(C_HUNK));
        assert_eq!(ls[1].spans[0].style.fg, Some(C_DEL));
        assert_eq!(ls[2].spans[0].style.fg, Some(C_ADD));
    }

    #[test]
    fn non_diff_body_is_dim_text() {
        let ls = render_diff_or_text("just some output");
        assert_eq!(ls[0].spans[0].style.fg, Some(C_DIM));
    }

    #[test]
    fn tool_result_ok_shows_check() {
        let ls = render_entry(&Entry::ToolResult { ok: true, body: String::new() });
        assert!(text_of(&ls[0]).contains("✅"));
    }

    #[test]
    fn spinner_cycles() {
        assert_eq!(spinner_frame(0), "⠋");
        assert_eq!(spinner_frame(10), "⠋");
        assert_ne!(spinner_frame(0), spinner_frame(1));
    }
}
