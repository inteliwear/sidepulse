//! Built-in and custom LED animation programs, independent of device output.

use std::io;

use sidepulse_core::AgentMode;

use crate::{apply_brightness, normalize_led_text, program_for_mode, validate_led_text};

pub const BUILTIN_ANIMATIONS: &[(&str, &str)] = &[
    ("idle-pulse", "Idle pulse"),
    ("cyan-roll", "Cyan roll"),
    ("kitt", "KITT"),
    ("kitt-red", "Red KITT"),
    ("ember-idle", "Ember idle"),
    ("ember-tide", "Ember tide"),
    ("ember-lid-open", "Ember lid open"),
    ("night-rider", "Night rider"),
    ("purple-idle", "Purple idle"),
    ("purple-tide", "Purple tide"),
    ("purple-lid-open", "Purple lid open"),
    ("lid-open", "Lid open"),
    ("lid-closed", "Lid closed"),
    ("amber-pulse", "Amber pulse"),
    ("solid-green", "Solid green"),
    ("cyan-complete", "Cyan complete"),
    ("ember-attention", "Ember attention"),
    ("ember-complete", "Ember complete"),
    ("purple-attention", "Purple attention"),
    ("purple-complete", "Purple complete"),
    ("off", "Fade off"),
    ("immediate-off", "Off"),
];

pub fn builtin_animation(id: &str, led_count: usize) -> Option<&'static str> {
    let dot = led_count == 2;
    Some(match (id, dot) {
        ("idle-pulse", true) => include_str!("../resources/animations/idle-pulse-2.LED"),
        ("idle-pulse", false) => include_str!("../resources/animations/idle-pulse-8.LED"),
        ("cyan-roll", true) => include_str!("../resources/animations/cyan-roll-2.LED"),
        ("cyan-roll", false) => include_str!("../resources/animations/cyan-roll-8.LED"),
        ("kitt", true) => include_str!("../resources/animations/kitt-2.LED"),
        ("kitt", false) => include_str!("../resources/animations/kitt-8.LED"),
        ("kitt-red", true) => include_str!("../resources/animations/kitt-red-2.LED"),
        ("kitt-red", false) => include_str!("../resources/animations/kitt-red-8.LED"),
        ("ember-idle", true) => include_str!("../resources/animations/ember-idle-2.LED"),
        ("ember-idle", false) => include_str!("../resources/animations/ember-idle-8.LED"),
        ("ember-tide", true) => include_str!("../resources/animations/ember-tide-2.LED"),
        ("ember-tide", false) => include_str!("../resources/animations/ember-tide-8.LED"),
        ("ember-lid-open", true) => include_str!("../resources/animations/ember-lid-open-2.LED"),
        ("ember-lid-open", false) => include_str!("../resources/animations/ember-lid-open-8.LED"),
        ("night-rider", true) => include_str!("../resources/animations/night-rider-2.LED"),
        ("night-rider", false) => include_str!("../resources/animations/night-rider-8.LED"),
        ("purple-idle", true) => include_str!("../resources/animations/purple-idle-2.LED"),
        ("purple-idle", false) => include_str!("../resources/animations/purple-idle-8.LED"),
        ("purple-tide", true) => include_str!("../resources/animations/purple-tide-2.LED"),
        ("purple-tide", false) => include_str!("../resources/animations/purple-tide-8.LED"),
        ("purple-lid-open", true) => include_str!("../resources/animations/purple-lid-open-2.LED"),
        ("purple-lid-open", false) => include_str!("../resources/animations/purple-lid-open-8.LED"),
        ("lid-open", true) => include_str!("../resources/animations/lid-open-2.LED"),
        ("lid-open", false) => include_str!("../resources/animations/lid-open-8.LED"),
        ("lid-closed", true) => include_str!("../resources/animations/lid-closed-2.LED"),
        ("lid-closed", false) => include_str!("../resources/animations/lid-closed-8.LED"),
        ("amber-pulse", _) => include_str!("../resources/animations/amber-pulse.LED"),
        ("solid-green", _) => include_str!("../resources/animations/solid-green.LED"),
        ("cyan-complete", _) => include_str!("../resources/animations/cyan-complete.LED"),
        ("ember-attention", _) => include_str!("../resources/animations/ember-attention.LED"),
        ("ember-complete", _) => include_str!("../resources/animations/ember-complete.LED"),
        ("purple-attention", _) => include_str!("../resources/animations/purple-attention.LED"),
        ("purple-complete", _) => include_str!("../resources/animations/purple-complete.LED"),
        ("off", _) => include_str!("../resources/animations/off.LED"),
        ("immediate-off", _) => include_str!("../resources/animations/immediate-off.LED"),
        _ => return None,
    })
}

pub fn program_for_style(
    mode: AgentMode,
    led_count: usize,
    brightness: u8,
    style: &str,
    custom_program: &str,
) -> io::Result<String> {
    if style == "default" {
        return Ok(program_for_mode(mode, led_count, brightness));
    }
    let source = if style == "custom" {
        let program = normalize_led_text(custom_program);
        if program.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "custom animation is empty",
            ));
        }
        program
    } else {
        builtin_animation(style, led_count)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "unknown animation style"))?
            .trim()
            .to_owned()
    };
    let result = apply_brightness(&source, brightness);
    validate_led_text(&result)?;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn every_bundled_program_matches_the_python_resource() {
        let root =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src/sidepulse/resources/animations");
        for entry in std::fs::read_dir(root).unwrap() {
            let entry = entry.unwrap();
            let name = entry.file_name().to_string_lossy().into_owned();
            if !name.ends_with(".LED") {
                continue;
            }
            let stem = name.trim_end_matches(".LED");
            let (id, count) = if let Some(id) = stem.strip_suffix("-2") {
                (id, 2)
            } else if let Some(id) = stem.strip_suffix("-8") {
                (id, 8)
            } else {
                (stem, 8)
            };
            let rust = builtin_animation(id, count).unwrap();
            assert_eq!(
                rust,
                std::fs::read_to_string(entry.path()).unwrap(),
                "{name}"
            );
        }
    }

    #[test]
    fn custom_program_normalizes_and_validates() {
        assert_eq!(
            program_for_style(AgentMode::Working, 8, 64, "custom", r"off\n#FF00FF").unwrap(),
            "brightness 64\noff\n#FF00FF"
        );
        assert!(program_for_style(AgentMode::Working, 8, 255, "custom", "").is_err());
        assert!(program_for_style(AgentMode::Working, 8, 255, "missing", "").is_err());
    }
}
