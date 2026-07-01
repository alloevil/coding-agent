//! Input composer — the bottom input box.
//!
//! Phase 2: single-line buffer with cursor, char insert / backspace / delete /
//! cursor movement. History and completion are pure-function-testable and land
//! in Phase 3/4; the buffer editing here is written so those extend cleanly.

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
}
