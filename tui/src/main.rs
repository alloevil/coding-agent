//! coding-agent-tui — full-screen Ratatui front-end.
//!
//! Spawns the Python protocol backend and drives it over stdin/stdout JSON,
//! rendering a full-screen TUI: scrollable transcript + bottom input box +
//! live streaming. See PROTOCOL.md for the wire format.

mod app;
mod backend;
mod composer;
mod file_index;
mod proto;
mod render;
mod setup;

use std::env;

use backend::Backend;
use proto::Request;

fn env_or(key: &str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_string())
}

/// Best-effort install dir when CODING_AGENT_DIR is unset: the binary lives at
/// <install>/tui/target/release/coding-agent-tui, so go three parents up.
/// Falls back to "." if the exe path can't be resolved.
fn default_install_dir() -> String {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent()            // release/
            .and_then(|p| p.parent())       // target/
            .and_then(|p| p.parent())       // tui/
            .and_then(|p| p.parent())       // <install>/
            .map(|p| p.to_path_buf()))
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|| ".".to_string())
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

    // Resolve where the Python core lives. The launcher sets these; when run
    // directly, fall back to the binary's own location (../../.. from
    // tui/target/release/) rather than the current working directory.
    let install_dir = env::var("CODING_AGENT_DIR").ok().unwrap_or_else(default_install_dir);
    let python = env::var("CODING_AGENT_PYTHON")
        .unwrap_or_else(|_| format!("{install_dir}/.venv/bin/python"));
    let cwd = install_dir;
    let model_hint = model.clone().unwrap_or_default();
    // --setup forces the config wizard even when already configured.
    let force_setup = env::args().any(|a| a == "--setup");

    let mut backend = Backend::spawn(&python, &cwd)?;
    // Always send init (even with an empty key) so the backend doesn't block
    // waiting for it. With no key the backend loads any saved config.json and
    // reports needs_setup via `ready`, and the TUI shows the setup wizard.
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

    app::run(backend, model_hint, force_setup).await
}

