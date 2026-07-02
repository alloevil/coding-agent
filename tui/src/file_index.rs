//! Lightweight workspace file index for `@file` completion (Claude Code parity).
//!
//! Scans the current directory once (bounded, skipping noise dirs) into a flat
//! list of relative paths, then offers fuzzy-prefix matches for an `@token`
//! typed in the composer. Kept pure/testable: `fuzzy_match` takes the file list
//! explicitly; `scan` does the IO.

use std::path::Path;

/// Directories never worth indexing (mirror the Python DEFAULT_IGNORE_DIRS).
const IGNORE_DIRS: &[&str] = &[
    ".git", "node_modules", "target", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".next", ".idea", ".cache",
];

/// Max files to index — keeps startup cheap on huge trees.
const MAX_FILES: usize = 5000;

/// Recursively collect relative file paths under `root` (bounded, noise-skipped).
pub fn scan(root: &Path) -> Vec<String> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        if out.len() >= MAX_FILES {
            break;
        }
        let Ok(entries) = std::fs::read_dir(&dir) else { continue };
        for entry in entries.flatten() {
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.starts_with('.') && name != ".env" {
                // skip dotfiles/dotdirs except a couple of common ones
                if path.is_dir() { continue; }
            }
            let Ok(ft) = entry.file_type() else { continue };
            if ft.is_dir() {
                if IGNORE_DIRS.contains(&name.as_str()) {
                    continue;
                }
                stack.push(path);
            } else if ft.is_file() {
                if let Ok(rel) = path.strip_prefix(root) {
                    out.push(rel.to_string_lossy().replace('\\', "/"));
                }
            }
        }
    }
    out.sort();
    out
}

/// Return up to `limit` files matching `query`, ranked: exact-substring in the
/// basename first, then anywhere in the path. Empty query → first `limit` files.
pub fn fuzzy_match<'a>(files: &'a [String], query: &str, limit: usize) -> Vec<&'a str> {
    let q = query.to_lowercase();
    if q.is_empty() {
        return files.iter().take(limit).map(|s| s.as_str()).collect();
    }
    let mut basename_hits = Vec::new();
    let mut path_hits = Vec::new();
    for f in files {
        let fl = f.to_lowercase();
        let base = fl.rsplit('/').next().unwrap_or(&fl);
        if base.contains(&q) {
            basename_hits.push(f.as_str());
        } else if fl.contains(&q) {
            path_hits.push(f.as_str());
        }
    }
    basename_hits.into_iter().chain(path_hits).take(limit).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_query_returns_prefix() {
        let files = vec!["a.rs".to_string(), "b.rs".to_string(), "c.rs".to_string()];
        assert_eq!(fuzzy_match(&files, "", 2), vec!["a.rs", "b.rs"]);
    }

    #[test]
    fn basename_hits_rank_before_path_hits() {
        let files = vec![
            "src/main.rs".to_string(),
            "docs/main_notes.md".to_string(),
            "mainlib/other.rs".to_string(), // "main" only in dir path
        ];
        let hits = fuzzy_match(&files, "main", 10);
        // basename matches (main.rs, main_notes.md) come before path-only (mainlib/other.rs)
        assert_eq!(hits[0], "src/main.rs");
        assert!(hits.contains(&"docs/main_notes.md"));
        assert_eq!(*hits.last().unwrap(), "mainlib/other.rs");
    }

    #[test]
    fn no_match_is_empty() {
        let files = vec!["a.rs".to_string()];
        assert!(fuzzy_match(&files, "zzz", 5).is_empty());
    }

    #[test]
    fn scan_skips_noise_and_finds_files() {
        let tmp = std::env::temp_dir().join(format!("ca-idx-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(tmp.join("src")).unwrap();
        std::fs::create_dir_all(tmp.join("node_modules/pkg")).unwrap();
        std::fs::write(tmp.join("src/main.rs"), "x").unwrap();
        std::fs::write(tmp.join("README.md"), "x").unwrap();
        std::fs::write(tmp.join("node_modules/pkg/index.js"), "x").unwrap();
        let files = scan(&tmp);
        assert!(files.contains(&"src/main.rs".to_string()));
        assert!(files.contains(&"README.md".to_string()));
        assert!(!files.iter().any(|f| f.contains("node_modules")), "noise dir skipped");
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
