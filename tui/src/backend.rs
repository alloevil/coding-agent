//! Backend bridge — spawns the Python agent core and talks the JSON protocol.
//!
//! Spawns `python -m coding_agent.protocol` as a child, writes [`Request`]s to
//! its stdin, and reads line-delimited [`Event`]s from its stdout on a task,
//! forwarding them to the UI over an mpsc channel.

use std::process::Stdio;

use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin};
use tokio::sync::mpsc;

use crate::proto::{Event, Request};

pub struct Backend {
    child: Child,
    stdin: ChildStdin,
    /// Receiver of events parsed from the child's stdout.
    pub events: mpsc::UnboundedReceiver<Event>,
}

impl Backend {
    /// Spawn the Python protocol backend. `python` is the interpreter path
    /// (e.g. the project venv's python), `cwd` its working directory.
    pub fn spawn(python: &str, cwd: &str) -> std::io::Result<Backend> {
        let mut child = tokio::process::Command::new(python)
            .args(["-m", "coding_agent.protocol"])
            .current_dir(cwd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // Capture backend stderr so a Python-side crash isn't lost/garbled
            // behind the alt-screen; forward it to the debug log if enabled.
            .stderr(Stdio::piped())
            .spawn()?;

        let stdin = child.stdin.take().expect("child stdin");
        let stdout = child.stdout.take().expect("child stdout");
        let stderr = child.stderr.take().expect("child stderr");

        // Drain the child's stderr into the debug log (if CODING_AGENT_DEBUG set).
        tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if let Ok(path) = std::env::var("CODING_AGENT_DEBUG") {
                    use std::io::Write;
                    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
                        let _ = writeln!(f, "[backend stderr] {line}");
                    }
                }
            }
        });

        let (tx, rx) = mpsc::unbounded_channel::<Event>();
        // Reader task: parse each stdout line into an Event, forward to UI.
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }
                if let Some(ev) = Event::from_line(line) {
                    if tx.send(ev).is_err() {
                        break; // UI dropped the receiver
                    }
                }
            }
        });

        Ok(Backend { child, stdin, events: rx })
    }

    /// Send a request as one JSON line to the child's stdin.
    pub async fn send(&mut self, req: &Request) -> std::io::Result<()> {
        let mut line = req.to_line();
        line.push('\n');
        self.stdin.write_all(line.as_bytes()).await?;
        self.stdin.flush().await
    }

    /// Terminate the child process.
    pub async fn shutdown(&mut self) {
        let _ = self.child.kill().await;
    }
}
