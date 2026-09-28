//! Service-owned profiles and named assets. New files are published before the
//! settings pointer changes; a conflict removes only files created by this save.

use super::{SettingsStore, write_atomic};
use serde_json::{Value, json};
use sidepulse_core::{
    ANIMATION_STATES, AnimationLibrary, AnimationLibraryEdit, AnimationProfile,
    AnimationProfileDocument, BUILTIN_PROFILE_IDS, CustomAnimation, builtin_animation_profiles,
    default_animation, unique_animation_id,
};
use sidepulse_device::{animations::builtin_animation, led_runtime::validate_program};
use std::{
    collections::BTreeMap,
    fs,
    io::{self, Read, Write},
    path::Path,
};

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}
fn grouped(selections: &mut BTreeMap<String, String>) {
    let working = selections["working"].clone();
    for state in ["tool_running", "long_task_progress"] {
        selections.insert(state.into(), working.clone());
    }
}
fn check_program(program: &str) -> io::Result<String> {
    let normalized = sidepulse_device::normalize_led_text(program);
    if normalized.is_empty() || normalized.len() > 65536 {
        return Err(invalid("custom animation is empty or too large"));
    }
    for count in [2, 8] {
        validate_program(&normalized, count)?;
    }
    Ok(normalized)
}

impl SettingsStore {
    pub(super) fn named_animation_program(&self, value: &Value) -> Option<String> {
        if let Some(file) = value.get("file").and_then(Value::as_str) {
            let relative = Path::new(file);
            if relative.file_name().is_some_and(|name| name == file)
                && relative
                    .extension()
                    .is_some_and(|extension| extension.eq_ignore_ascii_case("LED"))
            {
                let directory = self
                    .path
                    .parent()
                    .unwrap_or(Path::new("."))
                    .join("animations");
                let path = directory.join(file);
                if !fs::symlink_metadata(&directory)
                    .is_ok_and(|metadata| metadata.file_type().is_symlink())
                    && fs::symlink_metadata(&path).is_ok_and(|metadata| {
                        metadata.is_file() && !metadata.file_type().is_symlink()
                    })
                {
                    let mut text = String::new();
                    if fs::File::open(path)
                        .ok()?
                        .take(65537)
                        .read_to_string(&mut text)
                        .is_ok()
                        && text.len() <= 65536
                    {
                        return Some(text);
                    }
                }
            }
        }
        value
            .get("program")
            .and_then(Value::as_str)
            .filter(|program| program.len() <= 65536)
            .map(str::to_owned)
    }

    pub fn animation_library(&self) -> io::Result<AnimationLibrary> {
        let mut custom_animations = BTreeMap::new();
        if let Some(items) = self
            .document
            .get("custom_agent_animations")
            .and_then(Value::as_object)
        {
            for (id, value) in items {
                if sidepulse_core::validate_animation_id(id, "custom:").is_err() {
                    continue;
                }
                if let Some(program) = self.named_animation_program(value) {
                    custom_animations.insert(
                        id.clone(),
                        CustomAnimation {
                            name: value
                                .get("name")
                                .and_then(Value::as_str)
                                .unwrap_or(id)
                                .into(),
                            program,
                        },
                    );
                }
            }
        }
        let mut current = BTreeMap::new();
        for state in ANIMATION_STATES {
            let (style, program) = self.animation_for_state(state)?;
            let raw = self
                .document
                .get("agent_animations")
                .and_then(|items| items.get(state))
                .and_then(|setting| setting.get("style"))
                .and_then(Value::as_str);
            let selected = if let Some(id) = raw.filter(|id| custom_animations.contains_key(*id)) {
                id.to_owned()
            } else if style == "custom" {
                let name = format!("Current {}", state.replace('_', " "));
                let id =
                    unique_animation_id("custom:", &name, |id| custom_animations.contains_key(id));
                custom_animations.insert(id.clone(), CustomAnimation { name, program });
                id
            } else {
                style
            };
            current.insert(state.into(), selected);
        }
        grouped(&mut current);
        let mut profiles = builtin_animation_profiles();
        if let Some(items) = self
            .document
            .get("agent_animation_profiles")
            .and_then(Value::as_object)
        {
            for (id, value) in items {
                if BUILTIN_PROFILE_IDS.contains(&id.as_str())
                    || sidepulse_core::validate_animation_id(id, "profile:").is_err()
                {
                    continue;
                }
                let Ok(mut profile) = serde_json::from_value::<AnimationProfile>(value.clone())
                else {
                    continue;
                };
                if sidepulse_core::validate_animation_name(&profile.name).is_err() {
                    continue;
                }
                profile.animations = ANIMATION_STATES
                    .into_iter()
                    .map(|state| {
                        let style = profile
                            .animations
                            .get(state)
                            .filter(|style| {
                                builtin_animation(style, 8).is_some()
                                    || custom_animations.contains_key(*style)
                            })
                            .map_or_else(|| default_animation(state).to_owned(), Clone::clone);
                        (state.into(), style)
                    })
                    .collect();
                grouped(&mut profile.animations);
                profiles.insert(id.clone(), profile);
            }
        }
        let matching_profile = profiles
            .iter()
            .find(|(_, profile)| profile.animations == current)
            .map(|(id, _)| id.clone());
        Ok(AnimationLibrary {
            profiles,
            custom_animations,
            current,
            matching_profile,
        })
    }

    pub fn export_animation_profile(
        &self,
        id: Option<&str>,
    ) -> io::Result<AnimationProfileDocument> {
        let library = self.animation_library()?;
        let (name, animations) = if let Some(id) = id {
            let profile = library
                .profiles
                .get(id)
                .ok_or_else(|| invalid("unknown animation profile"))?;
            (profile.name.clone(), profile.animations.clone())
        } else {
            ("Current".into(), library.current)
        };
        let mut custom_animations = BTreeMap::new();
        for id in animations.values().filter(|id| id.starts_with("custom:")) {
            let custom = library
                .custom_animations
                .get(id)
                .ok_or_else(|| invalid("profile references a missing custom animation"))?;
            custom_animations.insert(id.clone(), custom.clone());
        }
        Ok(AnimationProfileDocument {
            format: "sidepulse-animation-profile".into(),
            version: 1,
            name,
            animations,
            custom_animations,
        })
    }

    pub fn edit_animation_library(&mut self, edit: &AnimationLibraryEdit) -> io::Result<()> {
        edit.validate().map_err(invalid)?;
        let mut library = self.animation_library()?;
        let mut updated = self.document.clone();
        let mut new_assets = BTreeMap::<String, CustomAnimation>::new();
        let mut apply = None;
        match edit {
            AnimationLibraryEdit::SaveProfile { id, name } => {
                let id = id.clone().unwrap_or_else(|| {
                    unique_animation_id("profile:", name, |id| library.profiles.contains_key(id))
                });
                if BUILTIN_PROFILE_IDS.contains(&id.as_str()) {
                    return Err(invalid("built-in profiles cannot be replaced"));
                }
                for id in library
                    .current
                    .values()
                    .filter(|id| id.starts_with("custom:"))
                {
                    if updated
                        .get("custom_agent_animations")
                        .is_none_or(|items| items.get(id).is_none())
                    {
                        new_assets.insert(id.clone(), library.custom_animations[id].clone());
                    }
                }
                let profile = AnimationProfile {
                    name: name.trim().into(),
                    animations: library.current.clone(),
                };
                updated
                    .entry("agent_animation_profiles")
                    .or_insert_with(|| json!({}))
                    .as_object_mut()
                    .ok_or_else(|| invalid("agent_animation_profiles must be an object"))?
                    .insert(id, json!(profile));
                apply = Some(profile.animations);
            }
            AnimationLibraryEdit::ApplyProfile { id } => {
                apply = Some(
                    library
                        .profiles
                        .get(id)
                        .ok_or_else(|| invalid("unknown animation profile"))?
                        .animations
                        .clone(),
                );
            }
            AnimationLibraryEdit::DeleteProfile { id } => {
                if BUILTIN_PROFILE_IDS.contains(&id.as_str()) {
                    return Err(invalid("built-in profiles cannot be deleted"));
                }
                if let Some(items) = updated
                    .get_mut("agent_animation_profiles")
                    .and_then(Value::as_object_mut)
                {
                    items.remove(id);
                }
            }
            AnimationLibraryEdit::SaveAnimation { id, name, program } => {
                let id = id.clone().unwrap_or_else(|| {
                    unique_animation_id("custom:", name, |id| {
                        library.custom_animations.contains_key(id)
                    })
                });
                new_assets.insert(
                    id,
                    CustomAnimation {
                        name: name.trim().into(),
                        program: check_program(program)?,
                    },
                );
            }
            AnimationLibraryEdit::DeleteAnimation { id } => {
                if library.current.values().any(|value| value == id)
                    || library
                        .profiles
                        .values()
                        .any(|profile| profile.animations.values().any(|value| value == id))
                {
                    return Err(invalid("animation is used by a status or profile"));
                }
                if let Some(items) = updated
                    .get_mut("custom_agent_animations")
                    .and_then(Value::as_object_mut)
                {
                    items.remove(id);
                }
            }
            AnimationLibraryEdit::ImportProfile { document } => {
                let mut remapped = BTreeMap::new();
                for (id, incoming) in &document.custom_animations {
                    let animation = CustomAnimation {
                        name: incoming.name.trim().into(),
                        program: check_program(&incoming.program)?,
                    };
                    let target = if library
                        .custom_animations
                        .get(id)
                        .is_some_and(|existing| *existing != animation)
                    {
                        unique_animation_id("custom:", &animation.name, |id| {
                            library.custom_animations.contains_key(id)
                        })
                    } else {
                        id.clone()
                    };
                    library
                        .custom_animations
                        .insert(target.clone(), animation.clone());
                    new_assets.insert(target.clone(), animation);
                    remapped.insert(id.clone(), target);
                }
                let mut selections = BTreeMap::new();
                for state in ANIMATION_STATES {
                    let id = document
                        .animations
                        .get(state)
                        .map_or(default_animation(state), String::as_str);
                    if builtin_animation(id, 8).is_none()
                        && !document.custom_animations.contains_key(id)
                    {
                        return Err(invalid(format!("unknown animation for {state}: {id}")));
                    }
                    selections.insert(
                        state.into(),
                        remapped.get(id).map_or_else(|| id.to_owned(), Clone::clone),
                    );
                }
                grouped(&mut selections);
                let id = unique_animation_id("profile:", &document.name, |id| {
                    library.profiles.contains_key(id)
                });
                updated
                    .entry("agent_animation_profiles")
                    .or_insert_with(|| json!({}))
                    .as_object_mut()
                    .ok_or_else(|| invalid("agent_animation_profiles must be an object"))?
                    .insert(
                        id,
                        json!(AnimationProfile {
                            name: document.name.trim().into(),
                            animations: selections.clone()
                        }),
                    );
                apply = Some(selections);
            }
        }
        if let Some(mut selections) = apply {
            grouped(&mut selections);
            let animations = updated
                .entry("agent_animations")
                .or_insert_with(|| json!({}))
                .as_object_mut()
                .ok_or_else(|| invalid("agent_animations must be an object"))?;
            for (state, style) in selections {
                let setting = animations
                    .entry(state)
                    .or_insert_with(|| json!({}))
                    .as_object_mut()
                    .ok_or_else(|| invalid("animation setting must be an object"))?;
                setting.insert("style".into(), json!(style));
                setting.remove("custom_program");
            }
        }
        let mut created = Vec::new();
        let result = (|| -> io::Result<Vec<u8>> {
            if !new_assets.is_empty() {
                let directory = self
                    .path
                    .parent()
                    .unwrap_or(Path::new("."))
                    .join("animations");
                if fs::symlink_metadata(&directory)
                    .is_ok_and(|metadata| metadata.file_type().is_symlink())
                {
                    return Err(invalid("refusing to write assets through a symlink"));
                }
                fs::create_dir_all(&directory)?;
                let items = updated
                    .entry("custom_agent_animations")
                    .or_insert_with(|| json!({}))
                    .as_object_mut()
                    .ok_or_else(|| invalid("custom_agent_animations must be an object"))?;
                for (id, animation) in new_assets {
                    let program = check_program(&animation.program)?;
                    let mut file = tempfile::Builder::new()
                        .prefix("sidepulse-")
                        .suffix(".LED")
                        .tempfile_in(&directory)?;
                    file.write_all(program.as_bytes())?;
                    file.as_file().sync_all()?;
                    let (_, path) = file.keep().map_err(|error| error.error)?;
                    created.push(path.clone());
                    let value = items
                        .entry(id)
                        .or_insert_with(|| json!({}))
                        .as_object_mut()
                        .ok_or_else(|| invalid("custom animation must be an object"))?;
                    value.insert("name".into(), json!(animation.name));
                    value.insert(
                        "file".into(),
                        json!(path.file_name().unwrap().to_string_lossy()),
                    );
                    value.remove("program");
                }
            }
            let candidate = Self {
                path: self.path.clone(),
                document: updated.clone(),
                original: self.original.clone(),
            };
            for state in ANIMATION_STATES {
                let (style, custom) = candidate.animation_for_state(state)?;
                for count in [2, 8] {
                    let program = sidepulse_device::animations::program_for_style(
                        sidepulse_core::AgentMode::IdleReady,
                        count,
                        255,
                        &style,
                        &custom,
                    )?;
                    validate_program(&program, count)?;
                }
            }
            write_atomic(
                &self.path,
                &Value::Object(updated.clone()),
                self.original.as_deref(),
            )
        })();
        match result {
            Ok(bytes) => {
                self.document = updated;
                self.original = Some(bytes);
                Ok(())
            }
            Err(error) => {
                for path in created {
                    let _ = fs::remove_file(path);
                }
                Err(error)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn conflicting_save_removes_new_files_and_preserves_previous_assets() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("settings.json");
        fs::write(&path, "{}").unwrap();
        let mut store = SettingsStore::load(&path).unwrap();
        let edit = |program: &str| AnimationLibraryEdit::SaveAnimation {
            id: Some("custom:existing".into()),
            name: "Existing".into(),
            program: program.into(),
        };
        store.edit_animation_library(&edit("#FF0080")).unwrap();
        let old_document = store.snapshot();
        let old_file = directory.path().join("animations").join(
            old_document["custom_agent_animations"]["custom:existing"]["file"]
                .as_str()
                .unwrap(),
        );
        fs::write(&path, r#"{"external":true}"#).unwrap();
        let error = store.edit_animation_library(&edit("#00FF00")).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::AlreadyExists);
        assert_eq!(fs::read_to_string(&old_file).unwrap(), "#FF0080");
        assert_eq!(
            fs::read_dir(directory.path().join("animations"))
                .unwrap()
                .count(),
            1
        );
        assert_eq!(store.snapshot(), old_document);
        assert_eq!(fs::read_to_string(path).unwrap(), r#"{"external":true}"#);
    }

    #[test]
    fn saving_inline_programs_creates_a_portable_named_profile() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("settings.json");
        fs::write(&path, r##"{"other":7,"agent_animations":{"working":{"style":"custom","custom_program":"#FF0080","other":"keep"}}}"##).unwrap();
        let mut store = SettingsStore::load(&path).unwrap();
        store
            .edit_animation_library(&AnimationLibraryEdit::SaveProfile {
                id: None,
                name: "My Profile".into(),
            })
            .unwrap();
        let document = store
            .export_animation_profile(Some("profile:my-profile"))
            .unwrap();
        document.validate().unwrap();
        let selected = &document.animations["working"];
        assert!(selected.starts_with("custom:"));
        assert_eq!(document.animations["tool_running"], *selected);
        assert_eq!(document.custom_animations[selected].program, "#FF0080");
        assert_eq!(store.snapshot()["other"], 7);
        assert_eq!(
            store.snapshot()["agent_animations"]["working"]["other"],
            "keep"
        );
        assert!(
            store.snapshot()["agent_animations"]["working"]
                .get("custom_program")
                .is_none()
        );
        assert_eq!(
            SettingsStore::load(&path)
                .unwrap()
                .export_animation_profile(Some("profile:my-profile"))
                .unwrap(),
            document
        );
    }

    #[cfg(unix)]
    #[test]
    fn named_asset_writes_refuse_a_symlink_directory() {
        let directory = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let path = directory.path().join("settings.json");
        fs::write(&path, "{}").unwrap();
        std::os::unix::fs::symlink(outside.path(), directory.path().join("animations")).unwrap();
        let mut store = SettingsStore::load(&path).unwrap();
        assert!(
            store
                .edit_animation_library(&AnimationLibraryEdit::SaveAnimation {
                    id: None,
                    name: "Test".into(),
                    program: "#123456".into()
                })
                .is_err()
        );
        assert_eq!(fs::read_dir(outside.path()).unwrap().count(), 0);
        assert_eq!(fs::read_to_string(path).unwrap(), "{}");
    }
}
