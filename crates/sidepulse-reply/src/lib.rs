//! Portable deterministic local reply classification. No UI, Python runtime,
//! remote inference, or monitor lifecycle dependency.
mod inference;
pub mod model_files;
pub use inference::ReplyClassifier;
use serde::{Deserialize, Serialize};
use std::sync::OnceLock;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Label {
    ReplyRequired,
    NoReplyRequired,
    Uncertain,
}
impl Label {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::ReplyRequired => "REPLY_REQUIRED",
            Self::NoReplyRequired => "NO_REPLY_REQUIRED",
            Self::Uncertain => "UNCERTAIN",
        }
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Classification {
    pub label: Label,
    pub raw_output: String,
}

/// Preserves the Python digit and named-label protocol, including ASCII label
/// boundaries and its uncertain fallback for ambiguous output.
pub fn parse_label(text: &str) -> Label {
    let text = text.trim();
    let mut chars = text.chars();
    if let Some(digit @ ('0' | '1' | '2')) = chars.next()
        && chars
            .next()
            .is_none_or(|character| character.is_whitespace() || ".!".contains(character))
    {
        return match digit {
            '0' => Label::NoReplyRequired,
            '1' => Label::ReplyRequired,
            _ => Label::Uncertain,
        };
    }
    static LABELS: OnceLock<regex::Regex> = OnceLock::new();
    let regex = LABELS
        .get_or_init(|| regex::Regex::new("NO_REPLY_REQUIRED|REPLY_REQUIRED|UNCERTAIN").unwrap());
    let upper = text.to_uppercase();
    let mut selected = None;
    for found in regex.find_iter(&upper) {
        let boundary = |character: char| character.is_ascii_uppercase() || character == '_';
        if upper[..found.start()]
            .chars()
            .next_back()
            .is_some_and(boundary)
            || upper[found.end()..].chars().next().is_some_and(boundary)
        {
            continue;
        }
        let label = match found.as_str() {
            "NO_REPLY_REQUIRED" => Label::NoReplyRequired,
            "REPLY_REQUIRED" => Label::ReplyRequired,
            _ => Label::Uncertain,
        };
        if selected.is_some_and(|selected| selected != label) {
            return Label::Uncertain;
        }
        selected = Some(label);
    }
    selected.unwrap_or(Label::Uncertain)
}
#[derive(Deserialize)]
struct Prompt {
    #[serde(rename = "SYSTEM_PROMPT")]
    system: String,
    #[serde(rename = "EXAMPLES")]
    examples: Vec<(String, String)>,
}
/// Qwen's chat template for this string-only, tool-free conversation.
pub fn build_prompt(message: &str) -> String {
    static PROMPT: OnceLock<Prompt> = OnceLock::new();
    let prompt = PROMPT
        .get_or_init(|| serde_json::from_str(include_str!("../resources/prompt.json")).unwrap());
    let mut text = format!("<|im_start|>system\n{}<|im_end|>\n", prompt.system);
    for (example, answer) in &prompt.examples {
        text.push_str(&format!("<|im_start|>user\nMessage:\n<message>\n{example}\n</message><|im_end|>\n<|im_start|>assistant\n{answer}<|im_end|>\n"));
    }
    text.push_str(&format!("<|im_start|>user\nMessage:\n<message>\n{message}\n</message><|im_end|>\n<|im_start|>assistant\n"));
    text
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn prompt_matches_the_pinned_qwen_template() {
        assert_eq!(
            build_prompt("Are you ready?"),
            include_str!("../resources/prompt-qwen-fixture.txt")
        );
    }
    #[test]
    fn python_output_parser_contract_is_preserved() {
        for (raw, expected) in [
            ("0", Label::NoReplyRequired),
            ("1\n", Label::ReplyRequired),
            ("2.", Label::Uncertain),
            ("01", Label::Uncertain),
            ("1! NO_REPLY_REQUIRED", Label::ReplyRequired),
            ("reply_required", Label::ReplyRequired),
            (
                "NO_REPLY_REQUIRED NO_REPLY_REQUIRED",
                Label::NoReplyRequired,
            ),
            ("REPLY_REQUIRED NO_REPLY_REQUIRED", Label::Uncertain),
            ("PREFIX_REPLY_REQUIRED", Label::Uncertain),
            ("REPLY_REQUIRED_SUFFIX", Label::Uncertain),
            ("雪REPLY_REQUIRED雪", Label::ReplyRequired),
            ("nothing", Label::Uncertain),
        ] {
            assert_eq!(parse_label(raw), expected, "{raw}");
        }
    }
}
