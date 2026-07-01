//! coding-agent-tui — Phase 1 skeleton.
//!
//! No UI yet: spawns the Python protocol backend, sends `init` + one
//! `user_input`, and prints streamed events until `done`. This proves the
//! stdin/stdout pipe end-to-end. Full-screen Ratatui UI lands in Phase 2.

mod backend;
mod proto;

use std::env;

use backend::Backend;
use proto::Request;

fn env_or(key: &str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_string())
}

#[tokio::main]
async fn main() -> std::io::Result<()> {
    // Config from env — Anthropic protocol endpoint or OpenAI-compatible.
    let (api_key, base_url, model, protocol, extra_headers) =
        if let Ok(tok) = env::var("ANTHROPIC_AUTH_TOKEN") {
            let mut headers = std::collections::HashMap::new();
            headers.insert("Authorization".to_string(), format!("Bearer {tok}"));
            (
                tok,
                env::var("ANTHROPIC_BASE_URL").ok(),
                Some(env_or("CODING_AGENT_MODEL", "claude-opus-4-8")),
                Some("anthropic".to_string()),
                Some(headers),
            )
        } else {
            (
                env_or("MODEL_API_KEY", &env_or("OPENAI_API_KEY", "")),
                env::var("MODEL_BASE_URL").ok().or_else(|| env::var("OPENAI_API_BASE").ok()),
                env::var("MODEL_PRIMARY").ok().or_else(|| env::var("CODING_AGENT_MODEL").ok()),
                None,
                None,
            )
        };

    if api_key.is_empty() {
        eprintln!("Error: set ANTHROPIC_AUTH_TOKEN (+ ANTHROPIC_BASE_URL) or MODEL_API_KEY/OPENAI_API_KEY");
        std::process::exit(1);
    }

    let python = env_or("CODING_AGENT_PYTHON", ".venv/bin/python");
    let cwd = env_or("CODING_AGENT_DIR", ".");

    let mut backend = Backend::spawn(&python, &cwd)?;

    // Init line first.
    backend
        .send(&Request::Init {
            api_key,
            model,
            api_base_url: base_url,
            auto_approve: true,
            protocol,
            extra_headers,
        })
        .await?;

    // Phase-1 smoke: a hardcoded task (overridable via arg).
    let task = env::args().nth(1).unwrap_or_else(|| "Say hello in one word.".to_string());

    // Drain events until we see `ready`, then submit the task.
    let mut sent = false;
    while let Some(ev) = backend.events.recv().await {
        match ev.kind.as_str() {
            "ready" => {
                eprintln!("[backend ready] model={}", ev.str_field("model").unwrap_or("?"));
                backend
                    .send(&Request::UserInput { content: task.clone(), session_id: None })
                    .await?;
                sent = true;
            }
            "stream_text" => {
                if let Some(t) = ev.str_field("text") {
                    print!("{t}");
                    use std::io::Write;
                    let _ = std::io::stdout().flush();
                }
            }
            "tool_call" => eprintln!("\n[tool_call] {}", ev.str_field("name").unwrap_or("?")),
            "tool_result" => eprintln!("[tool_result]"),
            "error" => eprintln!("\n[error] {}", ev.str_field("error").unwrap_or("?")),
            "session_state" | "done" => {
                if sent {
                    println!("\n[done]");
                    break;
                }
            }
            _ => {}
        }
    }

    backend.shutdown().await;
    Ok(())
}
