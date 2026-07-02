//! Protocol types — mirror of PROTOCOL.md.
//!
//! The Python core (`python -m coding_agent.protocol`) speaks line-delimited
//! JSON over stdin/stdout. Each message is a flat object with a `type` field.
//!
//! TUI -> Agent: [`Request`]   Agent -> TUI: [`Event`]
//!
//! This module is the single Rust-side source of truth for the wire format;
//! it must stay in sync with PROTOCOL.md.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Requests sent from the TUI to the agent (serialized as one JSON line).
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Request {
    /// First line: initialize config (api key / model / base url).
    Init {
        api_key: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        model: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        api_base_url: Option<String>,
        /// None = don't override the backend's configured value (config.json).
        #[serde(skip_serializing_if = "Option::is_none")]
        auto_approve: Option<bool>,
        /// Backend protocol: "openai" or "anthropic".
        #[serde(skip_serializing_if = "Option::is_none")]
        protocol: Option<String>,
        /// Extra HTTP headers (e.g. Authorization: Bearer ... for Anthropic gateways).
        #[serde(skip_serializing_if = "Option::is_none")]
        extra_headers: Option<std::collections::HashMap<String, String>>,
    },
    /// A user turn.
    UserInput {
        content: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        session_id: Option<String>,
    },
    /// Interrupt the running turn.
    Interrupt,
    /// Answer a pending permission request.
    PermissionResponse { approved: bool },
    /// Answer a pending ask_user question.
    QuestionResponse { answer: String },
    /// Start a fresh session.
    NewSession,
    /// List recent sessions.
    ListSessions,
    /// Toggle auto-approve.
    SetAutoApprove { value: bool },
    /// Guided setup: persist config to the global config.json.
    SaveConfig { answers: SaveAnswers },
}

/// Answers collected by the setup wizard, sent via SaveConfig.
#[derive(Debug, Clone, Default, Serialize)]
pub struct SaveAnswers {
    pub provider: String,
    pub api_key: String,
    pub model: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub base_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub protocol: Option<String>,
    pub auto_approve: bool,
}

impl Request {
    /// Serialize to a single JSON line (no trailing newline).
    pub fn to_line(&self) -> String {
        serde_json::to_string(self).unwrap_or_default()
    }
}

/// Events received from the agent. Unknown/extra fields are captured in `rest`
/// so the TUI is forward-compatible with protocol additions.
#[derive(Debug, Clone, Deserialize)]
pub struct Event {
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(flatten)]
    pub rest: Value,
}

impl Event {
    /// Parse one JSON line into an Event.
    pub fn from_line(line: &str) -> Option<Event> {
        serde_json::from_str(line).ok()
    }

    /// Convenience: a string field from the flattened payload.
    pub fn str_field(&self, key: &str) -> Option<&str> {
        self.rest.get(key).and_then(|v| v.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn user_input_serializes_with_type_tag() {
        let r = Request::UserInput { content: "hi".into(), session_id: None };
        let line = r.to_line();
        assert!(line.contains("\"type\":\"user_input\""));
        assert!(line.contains("\"content\":\"hi\""));
        // None session_id is omitted
        assert!(!line.contains("session_id"));
    }

    #[test]
    fn interrupt_serializes() {
        assert_eq!(Request::Interrupt.to_line(), "{\"type\":\"interrupt\"}");
    }

    #[test]
    fn init_omits_none_fields() {
        let r = Request::Init { api_key: "k".into(), model: None,
                                api_base_url: None, auto_approve: Some(true),
                                protocol: None, extra_headers: None };
        let line = r.to_line();
        assert!(line.contains("\"api_key\":\"k\""));
        assert!(!line.contains("model"));
        assert!(line.contains("\"auto_approve\":true"));
    }

    #[test]
    fn parse_stream_text_event() {
        let ev = Event::from_line("{\"type\":\"stream_text\",\"text\":\"hello\"}").unwrap();
        assert_eq!(ev.kind, "stream_text");
        assert_eq!(ev.str_field("text"), Some("hello"));
    }

    #[test]
    fn parse_tool_call_event_keeps_extra_fields() {
        let ev = Event::from_line(
            "{\"type\":\"tool_call\",\"name\":\"grep\",\"id\":\"1\",\"arguments\":{}}"
        ).unwrap();
        assert_eq!(ev.kind, "tool_call");
        assert_eq!(ev.str_field("name"), Some("grep"));
        assert_eq!(ev.str_field("id"), Some("1"));
    }

    #[test]
    fn parse_bad_line_returns_none() {
        assert!(Event::from_line("not json").is_none());
    }

    #[test]
    fn permission_response_roundtrip_shape() {
        let line = Request::PermissionResponse { approved: true }.to_line();
        assert!(line.contains("\"type\":\"permission_response\""));
        assert!(line.contains("\"approved\":true"));
    }

    #[test]
    fn save_config_serializes_answers() {
        let r = Request::SaveConfig {
            answers: SaveAnswers {
                provider: "anthropic".into(),
                api_key: "tok".into(),
                model: "claude-opus-4-8".into(),
                base_url: None,
                protocol: None,
                auto_approve: true,
            },
        };
        let line = r.to_line();
        assert!(line.contains("\"type\":\"save_config\""));
        assert!(line.contains("\"provider\":\"anthropic\""));
        assert!(line.contains("\"api_key\":\"tok\""));
        assert!(line.contains("\"auto_approve\":true"));
        assert!(!line.contains("base_url")); // None omitted
    }

    #[test]
    fn ready_needs_setup_parsed() {
        let ev = Event::from_line("{\"type\":\"ready\",\"model\":\"m\",\"needs_setup\":true}").unwrap();
        assert_eq!(ev.rest.get("needs_setup").and_then(|v| v.as_bool()), Some(true));
    }
}
