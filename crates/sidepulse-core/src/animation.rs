//! Animation profile documents shared across services and presentation clients.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const ANIMATION_STATES: [&str; 10] = [
    "idle_ready",
    "working",
    "tool_running",
    "waiting_for_input",
    "long_task_progress",
    "blocked_error",
    "completed",
    "unknown",
    "lid_open",
    "lid_closed",
];
pub const BUILTIN_PROFILE_IDS: [&str; 3] = ["profile:cyan", "profile:ember", "profile:purple"];

pub fn default_animation(state: &str) -> &'static str {
    match state {
        "working" | "tool_running" | "long_task_progress" => "cyan-roll",
        "waiting_for_input" | "blocked_error" => "amber-pulse",
        "completed" => "cyan-complete",
        "lid_open" => "lid-open",
        "lid_closed" => "lid-closed",
        _ => "idle-pulse",
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnimationProfile {
    pub name: String,
    pub animations: BTreeMap<String, String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CustomAnimation {
    pub name: String,
    pub program: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnimationProfileDocument {
    pub format: String,
    pub version: u16,
    pub name: String,
    pub animations: BTreeMap<String, String>,
    #[serde(default)]
    pub custom_animations: BTreeMap<String, CustomAnimation>,
}
impl AnimationProfileDocument {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.format != "sidepulse-animation-profile" || self.version != 1 {
            return Err("unsupported animation profile format or version");
        }
        validate_animation_name(&self.name)?;
        if !ANIMATION_STATES[..8]
            .iter()
            .all(|state| self.animations.contains_key(*state))
            || self
                .animations
                .keys()
                .any(|state| !ANIMATION_STATES.contains(&state.as_str()))
        {
            return Err("animation profile must include every agent state");
        }
        if self.custom_animations.len() > 100 {
            return Err("too many custom animations in profile");
        }
        for (id, animation) in &self.custom_animations {
            validate_animation_id(id, "custom:")?;
            validate_animation_name(&animation.name)?;
            if animation.program.is_empty() || animation.program.len() > 65536 {
                return Err("custom animation is empty or too large");
            }
        }
        if self
            .animations
            .values()
            .any(|value| value.is_empty() || value.len() > 128)
        {
            return Err("invalid profile animation selection");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnimationLibrary {
    pub profiles: BTreeMap<String, AnimationProfile>,
    pub custom_animations: BTreeMap<String, CustomAnimation>,
    pub current: BTreeMap<String, String>,
    pub matching_profile: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case")]
pub enum AnimationLibraryEdit {
    SaveProfile {
        id: Option<String>,
        name: String,
    },
    ApplyProfile {
        id: String,
    },
    DeleteProfile {
        id: String,
    },
    SaveAnimation {
        id: Option<String>,
        name: String,
        program: String,
    },
    DeleteAnimation {
        id: String,
    },
    ImportProfile {
        document: AnimationProfileDocument,
    },
}
impl AnimationLibraryEdit {
    pub fn validate(&self) -> Result<(), &'static str> {
        match self {
            Self::SaveProfile { id, name } => {
                validate_animation_name(name)?;
                if let Some(id) = id {
                    validate_animation_id(id, "profile:")?;
                }
            }
            Self::ApplyProfile { id } | Self::DeleteProfile { id } => {
                validate_animation_id(id, "profile:")?
            }
            Self::SaveAnimation { id, name, program } => {
                validate_animation_name(name)?;
                if let Some(id) = id {
                    validate_animation_id(id, "custom:")?;
                }
                if program.is_empty() || program.len() > 65536 {
                    return Err("custom animation is empty or too large");
                }
            }
            Self::DeleteAnimation { id } => validate_animation_id(id, "custom:")?,
            Self::ImportProfile { document } => document.validate()?,
        }
        Ok(())
    }
}

pub fn validate_animation_name(name: &str) -> Result<(), &'static str> {
    if name.trim().is_empty() || name.len() > 512 || name.contains('\0') {
        Err("animation name is empty or too large")
    } else {
        Ok(())
    }
}
pub fn validate_animation_id(id: &str, prefix: &str) -> Result<(), &'static str> {
    if id.len() > 128
        || !id.strip_prefix(prefix).is_some_and(|stem| {
            !stem.is_empty()
                && stem
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"-_".contains(&byte))
        })
    {
        Err("invalid animation identifier")
    } else {
        Ok(())
    }
}

pub fn unique_animation_id(prefix: &str, name: &str, used: impl Fn(&str) -> bool) -> String {
    let mut slug = String::new();
    for character in name.to_lowercase().chars() {
        if character.is_ascii_alphanumeric() {
            slug.push(character);
        } else if !slug.is_empty() && !slug.ends_with('-') {
            slug.push('-');
        }
        if slug.len() >= 80 {
            break;
        }
    }
    let slug = slug.trim_matches('-');
    let base = format!(
        "{prefix}{}",
        if slug.is_empty() { "animation" } else { slug }
    );
    let mut result = base.clone();
    let mut suffix = 2;
    while used(&result) {
        result = format!("{base}-{suffix}");
        suffix += 1;
    }
    result
}

pub fn builtin_animation_profiles() -> BTreeMap<String, AnimationProfile> {
    ["cyan", "ember", "purple"]
        .into_iter()
        .map(|color| {
            let animations = ANIMATION_STATES
                .into_iter()
                .map(|state| {
                    let style = if color == "cyan" || state == "lid_closed" {
                        default_animation(state).to_owned()
                    } else {
                        format!(
                            "{color}-{}",
                            match state {
                                "working" | "tool_running" | "long_task_progress" => "tide",
                                "waiting_for_input" | "blocked_error" => "attention",
                                "completed" => "complete",
                                "lid_open" => "lid-open",
                                _ => "idle",
                            }
                        )
                    };
                    (state.into(), style)
                })
                .collect();
            (
                format!("profile:{color}"),
                AnimationProfile {
                    name: match color {
                        "cyan" => "Cyan",
                        "ember" => "Ember",
                        _ => "Purple",
                    }
                    .into(),
                    animations,
                },
            )
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builtins_match_python_export_documents() {
        let profiles = builtin_animation_profiles();
        for (id, fixture) in [
            (
                "profile:cyan",
                include_str!("../resources/fixtures/animation-profile-cyan.json"),
            ),
            (
                "profile:ember",
                include_str!("../resources/fixtures/animation-profile-ember.json"),
            ),
            (
                "profile:purple",
                include_str!("../resources/fixtures/animation-profile-purple.json"),
            ),
        ] {
            let document: AnimationProfileDocument = serde_json::from_str(fixture).unwrap();
            document.validate().unwrap();
            assert_eq!(profiles[id].name, document.name);
            assert_eq!(profiles[id].animations, document.animations);
        }
    }
}
