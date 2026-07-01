//! Full-screen setup wizard — shown when the backend reports `needs_setup`.
//!
//! Walks the user through provider → (base_url for custom) → API key → model →
//! auto-approve, then emits a `SaveConfig` request. The step machine is pure
//! and unit-tested; ratatui rendering is in `render`.

use crate::composer::Composer;
use crate::proto::SaveAnswers;

/// Provider presets (mirror coding_agent/core/setup_wizard.PROVIDERS).
pub const PROVIDERS: &[(&str, &str, &str)] = &[
    // (id, label, default_model)
    ("openai", "OpenAI (api.openai.com)", "gpt-4o"),
    ("anthropic", "Anthropic / Claude (Messages API)", "claude-opus-4-8"),
    ("custom", "Custom OpenAI-compatible gateway", ""),
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Step {
    Provider,
    BaseUrl, // only for custom
    Key,
    Model,
    AutoApprove,
    Done,
}

pub struct Wizard {
    pub step: Step,
    pub provider_idx: usize,
    pub base_url: Composer,
    pub key: Composer,
    pub model: Composer,
    pub auto_approve: bool,
}

impl Wizard {
    pub fn new() -> Self {
        Wizard {
            step: Step::Provider,
            provider_idx: 0,
            base_url: Composer::new(),
            key: Composer::new(),
            model: Composer::new(),
            auto_approve: false,
        }
    }

    pub fn provider_id(&self) -> &'static str {
        PROVIDERS[self.provider_idx].0
    }

    fn is_custom(&self) -> bool {
        self.provider_id() == "custom"
    }

    pub fn provider_up(&mut self) {
        if self.provider_idx > 0 {
            self.provider_idx -= 1;
        }
    }

    pub fn provider_down(&mut self) {
        if self.provider_idx + 1 < PROVIDERS.len() {
            self.provider_idx += 1;
        }
    }

    /// Advance to the next step (called on Enter). Returns true when finished
    /// (step becomes Done) so the caller can emit SaveConfig.
    pub fn advance(&mut self) -> bool {
        self.step = match self.step {
            Step::Provider => {
                if self.is_custom() { Step::BaseUrl } else { Step::Key }
            }
            Step::BaseUrl => Step::Key,
            Step::Key => Step::Model,
            Step::Model => Step::AutoApprove,
            Step::AutoApprove => Step::Done,
            Step::Done => Step::Done,
        };
        self.step == Step::Done
    }

    /// Assemble the answers to send as SaveConfig.
    pub fn answers(&self) -> SaveAnswers {
        let model = {
            let m = self.model.text();
            if m.trim().is_empty() {
                PROVIDERS[self.provider_idx].2.to_string()
            } else {
                m
            }
        };
        SaveAnswers {
            provider: self.provider_id().to_string(),
            api_key: self.key.text().trim().to_string(),
            model,
            base_url: if self.is_custom() {
                Some(self.base_url.text().trim().to_string())
            } else {
                None
            },
            protocol: None, // Python preset decides; custom could extend later
            auto_approve: self.auto_approve,
        }
    }

    /// The composer for the current text-input step (if any).
    pub fn active_field(&mut self) -> Option<&mut Composer> {
        match self.step {
            Step::BaseUrl => Some(&mut self.base_url),
            Step::Key => Some(&mut self.key),
            Step::Model => Some(&mut self.model),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn openai_skips_base_url_step() {
        let mut w = Wizard::new(); // provider_idx 0 = openai
        assert_eq!(w.step, Step::Provider);
        w.advance();
        assert_eq!(w.step, Step::Key); // skipped BaseUrl
    }

    #[test]
    fn custom_includes_base_url_step() {
        let mut w = Wizard::new();
        w.provider_idx = 2; // custom
        w.advance();
        assert_eq!(w.step, Step::BaseUrl);
        w.advance();
        assert_eq!(w.step, Step::Key);
    }

    #[test]
    fn full_flow_reaches_done() {
        let mut w = Wizard::new();
        assert!(!w.advance()); // -> Key
        assert!(!w.advance()); // -> Model
        assert!(!w.advance()); // -> AutoApprove
        assert!(w.advance()); // -> Done (returns true)
        assert_eq!(w.step, Step::Done);
    }

    #[test]
    fn provider_nav_clamps() {
        let mut w = Wizard::new();
        w.provider_up(); // already at 0
        assert_eq!(w.provider_idx, 0);
        w.provider_down();
        w.provider_down();
        w.provider_down(); // clamp at last
        assert_eq!(w.provider_idx, PROVIDERS.len() - 1);
    }

    #[test]
    fn answers_uses_default_model_when_empty() {
        let mut w = Wizard::new();
        w.provider_idx = 1; // anthropic
        w.key.insert_str("tok");
        let a = w.answers();
        assert_eq!(a.provider, "anthropic");
        assert_eq!(a.api_key, "tok");
        assert_eq!(a.model, "claude-opus-4-8"); // default filled
        assert!(a.base_url.is_none());
    }

    #[test]
    fn answers_custom_includes_base_url() {
        let mut w = Wizard::new();
        w.provider_idx = 2;
        w.base_url.insert_str("https://gw/v1");
        w.key.insert_str("k");
        w.model.insert_str("m");
        let a = w.answers();
        assert_eq!(a.base_url.as_deref(), Some("https://gw/v1"));
        assert_eq!(a.model, "m");
    }

    #[test]
    fn active_field_tracks_step() {
        let mut w = Wizard::new();
        assert!(w.active_field().is_none()); // Provider: list, no text field
        w.step = Step::Key;
        assert!(w.active_field().is_some());
        w.step = Step::AutoApprove;
        assert!(w.active_field().is_none());
    }
}
