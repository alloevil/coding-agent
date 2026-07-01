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
}

impl Composer {
    pub fn new() -> Self {
        Composer { buf: Vec::new(), cursor: 0 }
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

    /// Take the current text and clear the buffer (on submit).
    pub fn take(&mut self) -> String {
        let s = self.text();
        self.buf.clear();
        self.cursor = 0;
        s
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
}
