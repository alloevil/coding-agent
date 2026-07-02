//! Input composer — the bottom input box.
//!
//! Single buffer with cursor supporting: char edit, cursor movement, input
//! history (↑/↓), multi-line (newline insert), paste, and Tab slash-command
//! completion. Pure/testable; the ratatui rendering lives in app.rs.

/// Known slash commands, for Tab completion. Mirrors core/commands.py BUILTINS.
pub const SLASH_COMMANDS: &[&str] = &[
    "help", "tools", "cost", "compact", "plan", "plan-mode", "agents", "agent",
    "model", "status", "config", "setup", "clear", "new", "sessions", "init",
    "quit", "exit",
];

/// A single-line editable input buffer with a cursor.
#[derive(Debug, Default, Clone)]
pub struct Composer {
    /// Current text (chars).
    buf: Vec<char>,
    /// Cursor position (0..=buf.len()).
    cursor: usize,
    /// Submitted-input history (oldest first).
    history: Vec<String>,
    /// History browse index: None = editing live draft; Some(i) = viewing history[i].
    hist_idx: Option<usize>,
    /// The live draft saved when history browsing began (restored on browse-past-end).
    saved_draft: String,
}

impl Composer {
    pub fn new() -> Self {
        Composer { buf: Vec::new(), cursor: 0, history: Vec::new(),
                   hist_idx: None, saved_draft: String::new() }
    }

    /// Replace the whole buffer, cursor to end. Used by history navigation.
    fn set_text(&mut self, s: &str) {
        self.buf = s.chars().collect();
        self.cursor = self.buf.len();
    }

    pub fn text(&self) -> String {
        self.buf.iter().collect()
    }

    #[allow(dead_code)] // used in tests; public accessor kept
    pub fn cursor(&self) -> usize {
        self.cursor
    }

    pub fn is_empty(&self) -> bool {
        self.buf.is_empty()
    }

    pub fn insert(&mut self, c: char) {
        self.hist_idx = None; // editing detaches from history browsing
        self.buf.insert(self.cursor, c);
        self.cursor += 1;
    }

    /// Insert a run of chars (e.g. a paste), advancing the cursor.
    pub fn insert_str(&mut self, s: &str) {
        for c in s.chars() {
            self.insert(c);
        }
    }

    pub fn backspace(&mut self) {
        if self.cursor > 0 {
            self.cursor -= 1;
            self.buf.remove(self.cursor);
        }
    }

    pub fn delete(&mut self) {
        if self.cursor < self.buf.len() {
            self.buf.remove(self.cursor);
        }
    }

    pub fn left(&mut self) {
        if self.cursor > 0 {
            self.cursor -= 1;
        }
    }

    pub fn right(&mut self) {
        if self.cursor < self.buf.len() {
            self.cursor += 1;
        }
    }

    pub fn home(&mut self) {
        self.cursor = 0;
    }

    pub fn end(&mut self) {
        self.cursor = self.buf.len();
    }

    /// Insert a newline (multi-line input). Enter submits; Shift/Alt+Enter calls this.
    pub fn newline(&mut self) {
        self.insert('\n');
    }

    /// Number of display lines in the buffer.
    pub fn line_count(&self) -> usize {
        self.text().split('\n').count()
    }

    /// If the buffer is a single line starting with `/`, try to Tab-complete the
    /// slash command. Returns true if it completed/advanced. Behavior:
    /// - unique prefix match → complete to that command + trailing space
    /// - multiple matches → complete to the longest common prefix
    /// - already a full command → no-op (false)
    pub fn complete_slash(&mut self) -> bool {
        let t = self.text();
        if !t.starts_with('/') || t.contains('\n') || t.contains(' ') {
            return false;
        }
        let typed = &t[1..];
        let matches: Vec<&str> = SLASH_COMMANDS
            .iter()
            .copied()
            .filter(|c| c.starts_with(typed))
            .collect();
        if matches.is_empty() {
            return false;
        }
        if matches.len() == 1 {
            self.set_text(&format!("/{} ", matches[0]));
            return matches[0] != typed;
        }
        // multiple → extend to the longest common prefix
        let lcp = longest_common_prefix(&matches);
        if lcp.len() > typed.len() {
            self.set_text(&format!("/{lcp}"));
            return true;
        }
        false
    }

    /// The current slash-command completion candidates (for a hint line).
    pub fn slash_candidates(&self) -> Vec<&'static str> {
        let t = self.text();
        if !t.starts_with('/') || t.contains('\n') || t.contains(' ') {
            return Vec::new();
        }
        let typed = &t[1..];
        SLASH_COMMANDS.iter().copied().filter(|c| c.starts_with(typed)).collect()
    }

    /// The in-progress `@file` token at the cursor, if any: the text after the
    /// most recent `@` (which must be at start or preceded by whitespace) up to
    /// the cursor, when it's a bare token. Drives the @file completion popup.
    pub fn at_token(&self) -> Option<String> {
        let upto: String = self.buf[..self.cursor].iter().collect();
        let at = upto.rfind('@')?;
        if at > 0 {
            if let Some(c) = upto[..at].chars().next_back() {
                if !c.is_whitespace() { return None; }
            }
        }
        let token = &upto[at + 1..];
        if token.contains(char::is_whitespace) {
            return None;
        }
        Some(token.to_string())
    }

    /// Replace the in-progress `@token` at the cursor with `@path ` (completed).
    pub fn complete_at(&mut self, path: &str) {
        let upto: String = self.buf[..self.cursor].iter().collect();
        if let Some(at) = upto.rfind('@') {
            let head = upto[..at].to_string();
            let rest: String = self.buf[self.cursor..].iter().collect();
            let new = format!("{head}@{path} {rest}");
            let new_cursor = head.chars().count() + 1 + path.chars().count() + 1;
            self.set_text(&new);
            self.cursor = new_cursor.min(self.buf.len());
        }
    }

    /// Discard the current draft without recording it into history (Esc-clear).
    pub fn clear(&mut self) {
        self.buf.clear();
        self.cursor = 0;
        self.hist_idx = None;
        self.saved_draft.clear();
    }

    /// Take the current text and clear the buffer (on submit). Records the
    /// text into history (skipping empties and exact-duplicate of last entry).
    pub fn take(&mut self) -> String {
        let s = self.text();
        if !s.trim().is_empty() && self.history.last().map(|h| h != &s).unwrap_or(true) {
            self.history.push(s.clone());
        }
        self.buf.clear();
        self.cursor = 0;
        self.hist_idx = None;
        self.saved_draft.clear();
        s
    }

    /// Recall the previous (older) history entry into the buffer (↑).
    pub fn history_prev(&mut self) {
        if self.history.is_empty() {
            return;
        }
        match self.hist_idx {
            None => {
                // Entering history: save the live draft, jump to newest entry.
                self.saved_draft = self.text();
                let i = self.history.len() - 1;
                self.hist_idx = Some(i);
                let t = self.history[i].clone();
                self.set_text(&t);
            }
            Some(0) => {} // already at oldest
            Some(i) => {
                let i = i - 1;
                self.hist_idx = Some(i);
                let t = self.history[i].clone();
                self.set_text(&t);
            }
        }
    }

    /// Recall the next (newer) history entry, or restore the draft past the end (↓).
    pub fn history_next(&mut self) {
        match self.hist_idx {
            None => {}
            Some(i) if i + 1 < self.history.len() => {
                let i = i + 1;
                self.hist_idx = Some(i);
                let t = self.history[i].clone();
                self.set_text(&t);
            }
            Some(_) => {
                // Past the newest entry → restore the saved live draft.
                self.hist_idx = None;
                let d = self.saved_draft.clone();
                self.set_text(&d);
            }
        }
    }
}

/// Longest common prefix of a set of strings (for Tab completion).
fn longest_common_prefix(items: &[&str]) -> String {
    if items.is_empty() {
        return String::new();
    }
    let first = items[0];
    let mut end = first.len();
    for s in &items[1..] {
        let common = first
            .char_indices()
            .zip(s.chars())
            .take_while(|((_, a), b)| a == b)
            .count();
        end = end.min(common);
    }
    first[..end].to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn insert_and_text() {
        let mut c = Composer::new();
        c.insert('h');
        c.insert('i');
        assert_eq!(c.text(), "hi");
        assert_eq!(c.cursor(), 2);
    }

    #[test]
    fn insert_at_cursor() {
        let mut c = Composer::new();
        c.insert_str("ac");
        c.left(); // cursor between a and c
        c.insert('b');
        assert_eq!(c.text(), "abc");
    }

    #[test]
    fn backspace_removes_before_cursor() {
        let mut c = Composer::new();
        c.insert_str("abc");
        c.backspace();
        assert_eq!(c.text(), "ab");
        c.home();
        c.backspace(); // at start: no-op
        assert_eq!(c.text(), "ab");
    }

    #[test]
    fn delete_removes_at_cursor() {
        let mut c = Composer::new();
        c.insert_str("abc");
        c.home();
        c.delete();
        assert_eq!(c.text(), "bc");
    }

    #[test]
    fn cursor_movement_bounds() {
        let mut c = Composer::new();
        c.insert_str("ab");
        c.right(); // already at end
        assert_eq!(c.cursor(), 2);
        c.home();
        c.left(); // already at start
        assert_eq!(c.cursor(), 0);
        c.end();
        assert_eq!(c.cursor(), 2);
    }

    #[test]
    fn take_clears() {
        let mut c = Composer::new();
        c.insert_str("hello");
        assert_eq!(c.take(), "hello");
        assert!(c.is_empty());
        assert_eq!(c.cursor(), 0);
    }

    #[test]
    fn paste_via_insert_str() {
        let mut c = Composer::new();
        c.insert_str("line one");
        assert_eq!(c.text(), "line one");
        assert_eq!(c.cursor(), 8);
    }

    #[test]
    fn history_records_on_take() {
        let mut c = Composer::new();
        c.insert_str("first");
        c.take();
        c.insert_str("second");
        c.take();
        // ↑ recalls newest first
        c.history_prev();
        assert_eq!(c.text(), "second");
        c.history_prev();
        assert_eq!(c.text(), "first");
        c.history_prev(); // clamp at oldest
        assert_eq!(c.text(), "first");
    }

    #[test]
    fn history_next_restores_draft() {
        let mut c = Composer::new();
        c.insert_str("old");
        c.take();
        c.insert_str("draft in progress");
        c.history_prev(); // save draft, show "old"
        assert_eq!(c.text(), "old");
        c.history_next(); // past newest → restore draft
        assert_eq!(c.text(), "draft in progress");
    }

    #[test]
    fn history_skips_empty_and_dupes() {
        let mut c = Composer::new();
        c.insert_str("   ");
        c.take(); // whitespace-only: not recorded
        c.insert_str("cmd");
        c.take();
        c.insert_str("cmd");
        c.take(); // duplicate of last: not recorded
        c.history_prev();
        assert_eq!(c.text(), "cmd");
        c.history_prev(); // only one entry
        assert_eq!(c.text(), "cmd");
    }

    #[test]
    fn typing_detaches_from_history() {
        let mut c = Composer::new();
        c.insert_str("past");
        c.take();
        c.history_prev();
        assert_eq!(c.text(), "past");
        c.insert('!'); // editing a recalled entry
        assert_eq!(c.text(), "past!");
        // now ↑ starts fresh from newest again (draft detached)
        c.history_prev();
        assert_eq!(c.text(), "past");
    }

    #[test]
    fn tab_completes_unique_prefix() {
        let mut c = Composer::new();
        c.insert_str("/mod");
        assert!(c.complete_slash());
        assert_eq!(c.text(), "/model "); // unique → full + space
    }

    #[test]
    fn tab_extends_to_common_prefix_when_ambiguous() {
        let mut c = Composer::new();
        c.insert_str("/pl"); // matches "plan" and "plan-mode"
        assert!(c.complete_slash());
        assert_eq!(c.text(), "/plan"); // common prefix
        // candidates still lists both
        let cands = c.slash_candidates();
        assert!(cands.contains(&"plan") && cands.contains(&"plan-mode"));
    }

    #[test]
    fn tab_noop_when_not_slash() {
        let mut c = Composer::new();
        c.insert_str("hello");
        assert!(!c.complete_slash());
        assert_eq!(c.text(), "hello");
    }

    #[test]
    fn slash_candidates_lists_all_for_bare_slash() {
        let mut c = Composer::new();
        c.insert_str("/");
        // bare "/" should surface the full command list (hint that / exists)
        assert!(c.slash_candidates().len() >= 3);
    }

    #[test]
    fn slash_candidates_single_match_for_unique_prefix() {
        let mut c = Composer::new();
        c.insert_str("/hel"); // only "/help"
        let cands = c.slash_candidates();
        assert_eq!(cands, vec!["help"]);
    }

    #[test]
    fn at_token_detects_mention() {
        let mut c = Composer::new();
        c.insert_str("look at @src/ma");
        assert_eq!(c.at_token().as_deref(), Some("src/ma"));
    }

    #[test]
    fn at_token_none_without_at_or_after_space() {
        let mut c = Composer::new();
        c.insert_str("hello world");
        assert!(c.at_token().is_none());
        // an email-like "a@b" (not preceded by space) is not a mention
        let mut c2 = Composer::new();
        c2.insert_str("user@host");
        assert!(c2.at_token().is_none());
    }

    #[test]
    fn complete_at_replaces_token_with_path() {
        let mut c = Composer::new();
        c.insert_str("edit @ma");
        c.complete_at("src/main.rs");
        assert_eq!(c.text(), "edit @src/main.rs ");
    }

    #[test]
    fn clear_discards_draft_without_history() {
        let mut c = Composer::new();
        c.insert_str("half-typed draft");
        c.clear();
        assert!(c.is_empty());
        // history untouched: ↑ recalls nothing
        c.history_prev();
        assert!(c.is_empty());
    }

    #[test]
    fn tab_noop_on_unknown_command() {
        let mut c = Composer::new();
        c.insert_str("/zzz");
        assert!(!c.complete_slash());
    }

    #[test]
    fn newline_makes_multiline() {
        let mut c = Composer::new();
        c.insert_str("line1");
        c.newline();
        c.insert_str("line2");
        assert_eq!(c.text(), "line1\nline2");
        assert_eq!(c.line_count(), 2);
    }

    #[test]
    fn slash_candidates_empty_after_space() {
        let mut c = Composer::new();
        c.insert_str("/model gpt");
        assert!(c.slash_candidates().is_empty()); // has a space → arg, not completing
    }

    #[test]
    fn lcp_helper() {
        assert_eq!(longest_common_prefix(&["plan", "plan-mode"]), "plan");
        assert_eq!(longest_common_prefix(&["abc", "xyz"]), "");
        assert_eq!(longest_common_prefix(&["only"]), "only");
    }
}
