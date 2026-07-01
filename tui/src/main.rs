//! coding-agent-tui — full-screen Ratatui front-end.
//!
//! Spawns the Python protocol backend and drives it over stdin/stdout JSON,
//! rendering a full-screen TUI: scrollable transcript + bottom input box +
//! live streaming. See PROTOCOL.md for the wire format.

mod app;
mod backend;
mod composer;
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
    let model_hint = model.clone().unwrap_or_default();

    let mut backend = Backend::spawn(&python, &cwd)?;
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

    app::run(backend, model_hint).await
}

