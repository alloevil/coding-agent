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

/// Compact elapsed formatting, matching Codex's fmt_elapsed_compact:
/// `<60s → "Ns"`, `<1h → "MmSs"`, else `"HhMm"`.
pub fn fmt_elapsed_compact(secs: u64) -> String {
    if secs < 60 {
        format!("{secs}s")
    } else if secs < 3600 {
        format!("{}m{:02}s", secs / 60, secs % 60)
    } else {
        format!("{}h{:02}m", secs / 3600, (secs % 3600) / 60)
    }
}

/// Shimmer text — a cosine highlight band sweeping across the characters,
/// replicating Codex's shimmer_spans (RGB-blended base↔highlight). `tick`
/// (bumped ~every 100ms) drives the sweep so the band moves left→right.
pub fn shimmer_spans(text: &str, tick: usize) -> Vec<Span<'static>> {
    let chars: Vec<char> = text.chars().collect();
    if chars.is_empty() {
        return Vec::new();
    }
    let padding = 10isize;
    let period = chars.len() as isize + padding * 2;
    // ~2s sweep at 100ms/tick → 20 ticks per sweep.
    let pos = ((tick as isize) % 20) * period / 20;
    let band_half = 5.0f32;
    // base grey → highlight near-white
    let base = (128u8, 128u8, 128u8);
    let hi = (235u8, 235u8, 235u8);
    let mut spans = Vec::with_capacity(chars.len());
    for (i, ch) in chars.iter().enumerate() {
        let i_pos = i as isize + padding;
        let dist = (i_pos - pos).abs() as f32;
        let t = if dist <= band_half {
            0.5 * (1.0 + (std::f32::consts::PI * (dist / band_half)).cos())
        } else {
            0.0
        };
        let blend = |a: u8, b: u8| -> u8 {
            (a as f32 + (b as f32 - a as f32) * (t * 0.9)).round() as u8
        };
        let (r, g, b) = (blend(base.0, hi.0), blend(base.1, hi.1), blend(base.2, hi.2));
        spans.push(Span::styled(ch.to_string(),
            Style::default().fg(Color::Rgb(r, g, b)).add_modifier(Modifier::BOLD)));
    }
    spans
}

/// ASCII wordmark for the welcome screen (our own art — not Codex's frames).
/// Rendered with an animated shimmer sweep, replicating Codex's animated
/// welcome banner behavior.
const BANNER: &[&str] = &[
    r"  ___         _ _                                 _   ",
    r" / __|___  __| (_)_ _  __ _   __ _ __ _ ___ _ _ | |_ ",
    r"| (__/ _ \/ _` | | ' \/ _` | / _` / _` / -_) ' \|  _|",
    r" \___\___/\__,_|_|_||_\__, | \__,_\__, \___|_||_|\__|",
    r"                      |___/       |___/              ",
];

/// Render the animated welcome banner (shimmering wordmark + tagline). `tick`
/// drives the shimmer. Falls back to a compact single line when the terminal is
/// too narrow (< banner width) — mirrors Codex skipping animation when small.
pub fn welcome_banner(tick: usize, width: u16) -> Vec<Line<'static>> {
    let banner_w = BANNER.iter().map(|l| l.chars().count()).max().unwrap_or(0) as u16;
    if width < banner_w + 2 {
        // Too narrow: compact greeting instead of clipped art.
        return vec![Line::from(Span::styled(
            "  coding-agent",
            Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD),
        ))];
    }
    let mut out: Vec<Line<'static>> = vec![Line::from("")];
    for (row, art) in BANNER.iter().enumerate() {
        // Offset the shimmer sweep per-row so the wave moves diagonally.
        out.push(Line::from(shimmer_spans(art, tick + row)));
    }
    out.push(Line::from(""));
    out.push(Line::from(Span::styled(
        "  your terminal coding agent",
        Style::default().fg(Color::DarkGray).add_modifier(Modifier::ITALIC),
    )));
    out
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

    #[test]
    fn elapsed_compact_formats() {
        assert_eq!(fmt_elapsed_compact(5), "5s");
        assert_eq!(fmt_elapsed_compact(59), "59s");
        assert_eq!(fmt_elapsed_compact(75), "1m15s");
        assert_eq!(fmt_elapsed_compact(3661), "1h01m");
    }

    #[test]
    fn shimmer_preserves_text_and_char_count() {
        let spans = shimmer_spans("Working", 3);
        let joined: String = spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(joined, "Working");
        assert_eq!(spans.len(), "Working".chars().count());
    }

    #[test]
    fn shimmer_empty_is_empty() {
        assert!(shimmer_spans("", 0).is_empty());
    }

    #[test]
    fn welcome_banner_full_width_has_art_and_tagline() {
        let ls = welcome_banner(0, 80);
        let joined: String = ls.iter().map(|l| text_of(l)).collect::<Vec<_>>().join("\n");
        assert!(joined.contains("your terminal coding agent"));
        assert!(ls.len() > 3, "full banner has multiple art rows");
    }

    #[test]
    fn welcome_banner_narrow_falls_back_to_compact() {
        let ls = welcome_banner(0, 20);
        assert_eq!(ls.len(), 1);
        assert!(text_of(&ls[0]).contains("coding-agent"));
    }
}
