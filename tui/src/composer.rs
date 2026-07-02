//! Input composer — the bottom input box.
//!
//! Single buffer with cursor supporting: char edit, cursor movement, input
//! history (↑/↓), multi-line (newline insert), paste, and Tab slash-command
//! completion. Pure/testable; the ratatui rendering lives in app.rs.

/// Known slash commands, for Tab completion. Mirrors core/commands.py BUILTINS.
pub const SLASH_COMMANDS: &[&str] = &[
    "help", "tools", "cost", "compact", "plan", "plan-mode", "agents", "agent",
    "model", "status", "config", "setup", "clear", "new", "sessions", "resume",
    "diff", "context", "recap", "review", "memory", "export", "undo", "mcp",
    "hooks", "doctor", "permissions", "vim", "init", "quit", "exit",
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
    /// Vim modal editing: off by default. When on, keys route through `vim_key`.
    vim_enabled: bool,
    /// Current vim sub-mode (only meaningful when `vim_enabled`).
    vim_mode: VimMode,
    /// Pending operator prefix (e.g. the first `d` of `dd`); cleared after use.
    vim_pending: Option<char>,
}

/// Vim sub-mode. Insert behaves like the normal composer; Normal routes motions.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum VimMode {
    #[default]
    Insert,
    Normal,
}

/// What `vim_key` did with a key, so the caller knows whether to fall through.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VimOutcome {
    /// Key was consumed by vim handling; caller should do nothing else.
    Consumed,
    /// Not in vim mode (or in Insert mode): caller handles the key normally.
    Passthrough,
}

impl Composer {
    pub fn new() -> Self {
        Composer { buf: Vec::new(), cursor: 0, history: Vec::new(),
                   hist_idx: None, saved_draft: String::new(),
                   vim_enabled: false, vim_mode: VimMode::Insert, vim_pending: None }
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

    // ── Vim modal editing ───────────────────────────────────────────────
    // Opt-in via `/vim`. When enabled, the composer starts in Normal mode;
    // motions/operators are handled by `vim_key`. Insert mode delegates to the
    // usual char/backspace/cursor methods, so nothing changes for non-vim users.

    /// Whether vim modal editing is enabled.
    pub fn vim_enabled(&self) -> bool {
        self.vim_enabled
    }

    /// The current vim sub-mode (Normal/Insert). Meaningful only when enabled.
    pub fn vim_mode(&self) -> VimMode {
        self.vim_mode
    }

    /// Toggle vim mode on/off. Turning on lands in Normal; turning off resets to
    /// a clean Insert state so a later re-enable is predictable. Returns the new
    /// enabled flag.
    pub fn toggle_vim(&mut self) -> bool {
        self.vim_enabled = !self.vim_enabled;
        self.vim_mode = if self.vim_enabled { VimMode::Normal } else { VimMode::Insert };
        self.vim_pending = None;
        self.vim_enabled
    }

    /// A short mode indicator for the UI (e.g. footer): "" when vim is off.
    pub fn vim_indicator(&self) -> &'static str {
        if !self.vim_enabled {
            ""
        } else {
            match self.vim_mode {
                VimMode::Normal => "NORMAL",
                VimMode::Insert => "INSERT",
            }
        }
    }

    /// Enter Insert mode (from Normal). No-op if already inserting.
    fn vim_to_insert(&mut self) {
        self.vim_mode = VimMode::Insert;
        self.vim_pending = None;
    }

    /// Move to the start of the next word (vim `w`): skip the current run of
    /// non-space, then any spaces.
    fn word_forward(&mut self) {
        self.cursor = self.next_word_start();
    }

    /// Index of the start of the next word from the cursor (vim `w` target).
    fn next_word_start(&self) -> usize {
        let n = self.buf.len();
        let mut i = self.cursor;
        if i >= n { return n; }
        let start_space = self.buf[i].is_whitespace();
        // advance over the current class
        while i < n && self.buf[i].is_whitespace() == start_space {
            i += 1;
        }
        // if we started on non-space, also skip the gap of spaces to the next word
        if !start_space {
            while i < n && self.buf[i].is_whitespace() {
                i += 1;
            }
        }
        i.min(n)
    }

    /// Index just past the end of the current/next word (vim `e` target, made
    /// exclusive so a delete `de` removes through that last char).
    fn word_end_excl(&self) -> usize {
        let n = self.buf.len();
        let mut i = self.cursor;
        if i >= n { return n; }
        // step forward at least one, skip any spaces to reach a word
        i += 1;
        while i < n && self.buf[i].is_whitespace() {
            i += 1;
        }
        // consume to the end of this word
        while i < n && !self.buf[i].is_whitespace() {
            i += 1;
        }
        i.min(n)
    }

    /// Move to the start of the previous word (vim `b`).
    fn word_back(&mut self) {
        if self.cursor == 0 { return; }
        let mut i = self.cursor - 1;
        // skip spaces to the left
        while i > 0 && self.buf[i].is_whitespace() {
            i -= 1;
        }
        // skip the word to its start
        while i > 0 && !self.buf[i - 1].is_whitespace() {
            i -= 1;
        }
        self.cursor = i;
    }

    /// Delete chars in [cursor, to) (to is exclusive, clamped). Used by operators.
    fn delete_to(&mut self, to: usize) {
        let to = to.min(self.buf.len());
        if to > self.cursor {
            self.buf.drain(self.cursor..to);
        }
    }

    /// Handle a single character key while vim mode is enabled. Returns a
    /// `VimOutcome` telling the caller how to proceed. In Insert mode everything
    /// except `Esc` (handled by the caller via `vim_escape`) is Passthrough.
    pub fn vim_key(&mut self, c: char) -> VimOutcome {
        if !self.vim_enabled || self.vim_mode == VimMode::Insert {
            return VimOutcome::Passthrough;
        }
        // Normal mode. Resolve a pending operator first.
        if let Some(op) = self.vim_pending.take() {
            // dd / cc: whole-line operators.
            if (op == 'd' && c == 'd') || (op == 'c' && c == 'c') {
                self.buf.clear();
                self.cursor = 0;
                if op == 'c' { self.vim_to_insert(); }
                return VimOutcome::Consumed;
            }
            // d/c + word motion (w → next-word-start, e → word-end).
            let target = match c {
                'w' => Some(self.next_word_start()),
                'e' => Some(self.word_end_excl()),
                _ => None,
            };
            if let Some(to) = target {
                self.delete_to(to);
                if op == 'c' { self.vim_to_insert(); }
                return VimOutcome::Consumed;
            }
            // Any other second key cancels the operator; fall through to treat
            // `c` as a fresh Normal command.
        }
        match c {
            'i' => self.vim_to_insert(),
            'a' => { self.right(); self.vim_to_insert(); }
            'A' => { self.end(); self.vim_to_insert(); }
            'I' => { self.home(); self.vim_to_insert(); }
            'o' => { self.end(); self.newline(); self.vim_to_insert(); }
            'h' => self.left(),
            'l' => self.right(),
            '0' => self.home(),
            '$' => self.end(),
            'w' => self.word_forward(),
            'b' => self.word_back(),
            'e' => {
                // `e` motion lands ON the last char of the word (exclusive-1).
                let end = self.word_end_excl();
                self.cursor = end.saturating_sub(1).max(self.cursor.min(end));
            }
            'x' => self.delete(),
            'D' => { self.buf.truncate(self.cursor); }
            'C' => { self.buf.truncate(self.cursor); self.vim_to_insert(); }
            'd' => { self.vim_pending = Some('d'); }
            'c' => { self.vim_pending = Some('c'); }
            // j/k browse history, matching the arrow keys — handy in Normal mode.
            'k' => self.history_prev(),
            'j' => self.history_next(),
            _ => {} // unmapped Normal-mode keys are swallowed (vim-like)
        }
        VimOutcome::Consumed
    }

    /// Esc in vim mode: leave Insert for Normal (and clear any pending op).
    /// Returns true if it did something vim-specific (so the caller skips its
    /// own Esc handling). When vim is off, returns false.
    pub fn vim_escape(&mut self) -> bool {
        if !self.vim_enabled {
            return false;
        }
        self.vim_mode = VimMode::Normal;
        self.vim_pending = None;
        true
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

    // ── Vim mode ─────────────────────────────────────────────────────────

    #[test]
    fn vim_off_by_default_passes_through() {
        let mut c = Composer::new();
        assert!(!c.vim_enabled());
        assert_eq!(c.vim_key('i'), VimOutcome::Passthrough);
        assert_eq!(c.vim_indicator(), "");
        assert!(!c.vim_escape()); // no-op when off
    }

    #[test]
    fn vim_toggle_starts_in_normal() {
        let mut c = Composer::new();
        assert!(c.toggle_vim());
        assert!(c.vim_enabled());
        assert_eq!(c.vim_mode(), VimMode::Normal);
        assert_eq!(c.vim_indicator(), "NORMAL");
        // toggling off resets to a clean insert state
        assert!(!c.toggle_vim());
        assert_eq!(c.vim_mode(), VimMode::Insert);
        assert_eq!(c.vim_indicator(), "");
    }

    #[test]
    fn vim_insert_mode_passes_through_chars() {
        let mut c = Composer::new();
        c.toggle_vim();
        c.vim_key('i'); // Normal 'i' → Insert
        assert_eq!(c.vim_mode(), VimMode::Insert);
        // now chars are passthrough (caller inserts them)
        assert_eq!(c.vim_key('x'), VimOutcome::Passthrough);
    }

    #[test]
    fn vim_i_a_enter_insert_at_right_place() {
        let mut c = Composer::new();
        c.insert_str("abc");
        c.toggle_vim();       // Normal, cursor at end (3)
        c.home();             // cursor 0
        assert_eq!(c.vim_key('a'), VimOutcome::Consumed); // append → cursor 1, Insert
        assert_eq!(c.cursor(), 1);
        assert_eq!(c.vim_mode(), VimMode::Insert);
    }

    #[test]
    fn vim_motions_hjkl_and_word() {
        let mut c = Composer::new();
        c.insert_str("foo bar baz");
        c.toggle_vim();
        c.vim_key('0'); // home
        assert_eq!(c.cursor(), 0);
        c.vim_key('w'); // → start of "bar"
        assert_eq!(c.cursor(), 4);
        c.vim_key('w'); // → start of "baz"
        assert_eq!(c.cursor(), 8);
        c.vim_key('b'); // back → start of "bar"
        assert_eq!(c.cursor(), 4);
        c.vim_key('l'); // right one
        assert_eq!(c.cursor(), 5);
        c.vim_key('h'); // left one
        assert_eq!(c.cursor(), 4);
        c.vim_key('$'); // end
        assert_eq!(c.cursor(), 11);
    }

    #[test]
    fn vim_x_deletes_and_dd_clears_line() {
        let mut c = Composer::new();
        c.insert_str("hello");
        c.toggle_vim();
        c.vim_key('0');
        c.vim_key('x'); // delete 'h'
        assert_eq!(c.text(), "ello");
        // dd clears the whole line
        c.vim_key('d');
        c.vim_key('d');
        assert_eq!(c.text(), "");
        assert_eq!(c.cursor(), 0);
    }

    #[test]
    fn vim_capital_d_kills_to_end() {
        let mut c = Composer::new();
        c.insert_str("keep DROP");
        c.toggle_vim();
        c.vim_key('0');
        for _ in 0..5 { c.vim_key('l'); } // cursor at 'D' (index 5)
        c.vim_key('D');
        assert_eq!(c.text(), "keep ");
    }

    #[test]
    fn vim_escape_returns_to_normal() {
        let mut c = Composer::new();
        c.toggle_vim();
        c.vim_key('i'); // Insert
        assert_eq!(c.vim_mode(), VimMode::Insert);
        assert!(c.vim_escape()); // back to Normal, consumed
        assert_eq!(c.vim_mode(), VimMode::Normal);
    }

    #[test]
    fn vim_pending_operator_cancels_on_other_key() {
        let mut c = Composer::new();
        c.insert_str("abcd");
        c.toggle_vim();
        c.vim_key('0');
        c.vim_key('d');  // pending operator
        c.vim_key('l');  // not 'd' → cancels; treated as motion right
        assert_eq!(c.text(), "abcd"); // nothing deleted
        assert_eq!(c.cursor(), 1);
    }

    #[test]
    fn vim_dw_deletes_word_and_trailing_space() {
        let mut c = Composer::new();
        c.insert_str("foo bar baz");
        c.toggle_vim();
        c.vim_key('0');
        c.vim_key('d');
        c.vim_key('w'); // delete "foo " → "bar baz"
        assert_eq!(c.text(), "bar baz");
        assert_eq!(c.cursor(), 0);
    }

    #[test]
    fn vim_de_deletes_to_word_end() {
        let mut c = Composer::new();
        c.insert_str("foo bar");
        c.toggle_vim();
        c.vim_key('0');
        c.vim_key('d');
        c.vim_key('e'); // delete "foo" (keeps the space) → " bar"
        assert_eq!(c.text(), " bar");
    }

    #[test]
    fn vim_cw_deletes_word_and_enters_insert() {
        let mut c = Composer::new();
        c.insert_str("foo bar");
        c.toggle_vim();
        c.vim_key('0');
        c.vim_key('c');
        c.vim_key('w'); // change word: delete "foo " and enter Insert
        assert_eq!(c.text(), "bar");
        assert_eq!(c.vim_mode(), VimMode::Insert);
    }

    #[test]
    fn vim_cc_clears_line_into_insert() {
        let mut c = Composer::new();
        c.insert_str("throwaway");
        c.toggle_vim();
        c.vim_key('c');
        c.vim_key('c');
        assert_eq!(c.text(), "");
        assert_eq!(c.vim_mode(), VimMode::Insert);
    }

    #[test]
    fn vim_capital_c_kills_to_end_into_insert() {
        let mut c = Composer::new();
        c.insert_str("keep DROP");
        c.toggle_vim();
        c.vim_key('0');
        for _ in 0..5 { c.vim_key('l'); }
        c.vim_key('C'); // kill to EOL and insert
        assert_eq!(c.text(), "keep ");
        assert_eq!(c.vim_mode(), VimMode::Insert);
    }

    #[test]
    fn vim_e_motion_lands_on_word_end() {
        let mut c = Composer::new();
        c.insert_str("foo bar");
        c.toggle_vim();
        c.vim_key('0');
        c.vim_key('e'); // land on last char of "foo" → index 2
        assert_eq!(c.cursor(), 2);
    }
}
