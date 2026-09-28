//! Legacy settings document owned by the Rust service. Unknown fields are
//! retained so a development service can update one setting safely.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};
#[cfg(any(target_os = "macos", test))]
use sidepulse_core::AwakePolicy;
#[cfg(any(target_os = "macos", test))]
use sidepulse_core::SleepSettingsPatch;
use sidepulse_core::{
    AgentListSettingsPatch, AgentMode, BatterySettingsPatch, ChargerBaseline, MonitoringPolicy,
};
use sidepulse_device::animations::{BUILTIN_ANIMATIONS, builtin_animation, program_for_style};
use sidepulse_device::target_from_device_path;
use tempfile::NamedTempFile;

pub struct SettingsStore {
    path: PathBuf,
    document: Map<String, Value>,
    original: Option<Vec<u8>>,
}

impl SettingsStore {
    pub fn load(path: &Path) -> io::Result<Self> {
        let original = match fs::read(path) {
            Ok(bytes) => Some(bytes),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        let document = match &original {
            Some(bytes) => serde_json::from_slice::<Value>(bytes)
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?
                .as_object()
                .cloned()
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "settings must be a JSON object")
                })?,
            None => Map::new(),
        };
        Ok(Self {
            path: path.to_path_buf(),
            document,
            original,
        })
    }

    pub fn snapshot(&self) -> Value {
        Value::Object(self.document.clone())
    }

    pub fn brightness_for_device(&self, path: &Path) -> u8 {
        self.find_device(path)
            .and_then(|device| device.get("brightness"))
            .and_then(Value::as_u64)
            .and_then(|brightness| u8::try_from(brightness).ok())
            .unwrap_or(255)
    }

    pub fn display_for_device(&self, path: &Path) -> &str {
        self.find_device(path)
            .and_then(|device| device.get("led_display"))
            .and_then(Value::as_str)
            .or_else(|| self.document.get("led_display").and_then(Value::as_str))
            .unwrap_or("agent")
    }

    pub fn battery_full_charge_watts(&self) -> Option<f64> {
        self.document
            .get("battery_monitoring")
            .and_then(|battery| battery.get("full_charge_watts"))
            .and_then(Value::as_f64)
            .filter(|watts| watts.is_finite() && *watts > 0.0)
    }

    pub fn battery_preview_settings(&self) -> (bool, f64) {
        let battery = self.document.get("battery_monitoring");
        let enabled = battery
            .and_then(|value| value.get("show_on_power_change"))
            .and_then(Value::as_bool)
            .unwrap_or(true);
        let seconds = battery
            .and_then(|value| value.get("power_change_preview_seconds"))
            .and_then(Value::as_f64)
            .filter(|seconds| seconds.is_finite())
            .unwrap_or(7.0)
            .max(0.0);
        (enabled, seconds)
    }

    pub fn set_battery_settings(&mut self, patch: &BatterySettingsPatch) -> io::Result<()> {
        patch
            .validate()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        let mut updated = self.document.clone();
        if let Some(mode) = &patch.display {
            updated.insert("led_display".into(), json!(mode));
        }
        let battery = updated
            .entry("battery_monitoring")
            .or_insert_with(|| json!({}));
        let object = battery.as_object_mut().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "battery_monitoring must be an object",
            )
        })?;
        if let Some(baseline) = patch.full_charge_watts {
            object.insert(
                "full_charge_watts".into(),
                match baseline {
                    ChargerBaseline::Auto => Value::Null,
                    ChargerBaseline::Watts { watts } => json!(watts),
                },
            );
        }
        if let Some(enabled) = patch.show_on_power_change {
            object.insert("show_on_power_change".into(), json!(enabled));
        }
        if let Some(seconds) = patch.power_change_preview_seconds {
            object.insert("power_change_preview_seconds".into(), json!(seconds));
        }
        self.original = Some(write_atomic(
            &self.path,
            &Value::Object(updated.clone()),
            self.original.as_deref(),
        )?);
        self.document = updated;
        Ok(())
    }

    #[cfg(any(target_os = "macos", test))]
    pub fn sleep_policy(&self) -> AwakePolicy {
        match self
            .document
            .get("sleep_prevention_policy")
            .and_then(Value::as_str)
        {
            Some("never") => AwakePolicy::Never,
            Some("always") => AwakePolicy::Always,
            _ => AwakePolicy::Agents,
        }
    }

    pub fn monitoring_policy(&self) -> MonitoringPolicy {
        let seconds = self
            .document
            .get("agent_list")
            .and_then(|value| value.get("idle_timeout_seconds"))
            .or_else(|| self.document.get("idle_timeout_seconds"))
            .and_then(Value::as_f64)
            .filter(|seconds| seconds.is_finite())
            .unwrap_or(3600.0)
            .max(0.0);
        MonitoringPolicy {
            stale_after_seconds: seconds,
            ..Default::default()
        }
    }

    pub fn set_agent_list_settings(&mut self, patch: &AgentListSettingsPatch) -> io::Result<()> {
        patch
            .validate()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        let mut updated = self.document.clone();
        let agent_list = updated
            .entry("agent_list")
            .or_insert_with(|| json!({}))
            .as_object_mut()
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "agent_list must be an object")
            })?;
        if let Some(seconds) = patch.idle_timeout_seconds {
            agent_list.insert("idle_timeout_seconds".into(), json!(seconds));
        }
        if let Some(seconds) = patch.recent_session_retention_seconds {
            agent_list.insert("recent_session_retention_seconds".into(), json!(seconds));
        }
        self.original = Some(write_atomic(
            &self.path,
            &Value::Object(updated.clone()),
            self.original.as_deref(),
        )?);
        self.document = updated;
        Ok(())
    }

    #[cfg(any(target_os = "macos", test))]
    pub fn set_sleep_settings(&mut self, patch: &SleepSettingsPatch) -> io::Result<()> {
        patch
            .validate()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        let mut updated = self.document.clone();
        if let Some(policy) = &patch.policy {
            updated.insert("sleep_prevention_policy".into(), json!(policy));
        }
        if let Some(percent) = patch.min_battery_percent {
            let sleep = updated
                .entry("sleep_prevention")
                .or_insert_with(|| json!({}))
                .as_object_mut()
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "sleep_prevention must be an object",
                    )
                })?;
            sleep.insert("min_battery_percent".into(), json!(percent));
        }
        self.original = Some(write_atomic(
            &self.path,
            &Value::Object(updated.clone()),
            self.original.as_deref(),
        )?);
        self.document = updated;
        Ok(())
    }

    #[cfg(any(target_os = "macos", test))]
    pub fn sleep_battery_threshold(&self) -> f64 {
        self.document
            .get("sleep_prevention")
            .and_then(|settings| settings.get("min_battery_percent"))
            .and_then(Value::as_f64)
            .or_else(|| {
                self.document
                    .get("sleep_prevention_min_battery_percent")
                    .and_then(Value::as_f64)
            })
            .filter(|value| value.is_finite())
            .unwrap_or(20.0)
            .clamp(0.0, 100.0)
    }

    #[cfg(any(target_os = "macos", test))]
    pub fn closed_lid_system_override_enabled(&self) -> bool {
        self.document
            .get("closed_lid_system_override_enabled")
            .and_then(Value::as_bool)
            .unwrap_or(false)
    }

    #[cfg(any(target_os = "macos", test))]
    pub fn set_sleep_policy(&mut self, policy: &str) -> io::Result<()> {
        self.set_sleep_settings(&SleepSettingsPatch {
            policy: Some(policy.into()),
            ..Default::default()
        })
    }

    pub fn transcript_enabled(&self, provider: &str) -> bool {
        self.document
            .get("transcript_monitoring")
            .and_then(|monitoring| monitoring.get(provider))
            .and_then(Value::as_bool)
            .unwrap_or(false)
    }

    pub fn set_transcript_enabled(&mut self, provider: &str, enabled: bool) -> io::Result<()> {
        if !matches!(provider, "codex" | "claude") {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid transcript provider",
            ));
        }
        let mut updated = self.document.clone();
        let monitoring = updated
            .entry("transcript_monitoring")
            .or_insert_with(|| json!({}));
        let object = monitoring.as_object_mut().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "transcript_monitoring must be an object",
            )
        })?;
        object.insert(provider.to_owned(), json!(enabled));
        self.original = Some(write_atomic(
            &self.path,
            &Value::Object(updated.clone()),
            self.original.as_deref(),
        )?);
        self.document = updated;
        Ok(())
    }

    pub fn animation_for_mode(&self, mode: AgentMode) -> io::Result<(String, String)> {
        let key = mode.key();
        let default = match mode {
            AgentMode::Working | AgentMode::ToolRunning | AgentMode::LongTaskProgress => {
                "cyan-roll"
            }
            AgentMode::WaitingForInput | AgentMode::BlockedError => "amber-pulse",
            AgentMode::Completed => "cyan-complete",
            _ => "idle-pulse",
        };
        let selected = self
            .document
            .get("agent_animations")
            .and_then(|animations| {
                animations.get(key).or_else(|| {
                    matches!(mode, AgentMode::ToolRunning | AgentMode::LongTaskProgress)
                        .then(|| animations.get("working"))
                        .flatten()
                })
            })
            .and_then(|setting| setting.get("style"))
            .and_then(Value::as_str)
            .unwrap_or(default);
        if selected == "default" {
            return Ok((default.to_owned(), String::new()));
        }
        if selected == "custom" {
            let program = self
                .document
                .get("agent_animations")
                .and_then(|animations| animations.get(key))
                .and_then(|setting| setting.get("custom_program"))
                .and_then(Value::as_str)
                .unwrap_or("");
            return Ok(("custom".into(), program.to_owned()));
        }
        if selected.starts_with("custom:") {
            let Some(custom) = self
                .document
                .get("custom_agent_animations")
                .and_then(|items| items.get(selected))
            else {
                return Ok((default.to_owned(), String::new()));
            };
            let file = custom.get("file").and_then(Value::as_str);
            let file_program = file.and_then(|file| {
                let path = Path::new(file);
                (path.file_name().is_some_and(|name| name == file)
                    && path
                        .extension()
                        .is_some_and(|extension| extension.eq_ignore_ascii_case("LED")))
                .then(|| {
                    self.path
                        .parent()
                        .unwrap_or_else(|| Path::new("."))
                        .join("animations")
                        .join(file)
                })
            });
            let program = if let Some(path) = file_program {
                fs::read_to_string(path).ok().or_else(|| {
                    custom
                        .get("program")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                })
            } else {
                custom
                    .get("program")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            };
            return Ok(program.map_or_else(
                || (default.to_owned(), String::new()),
                |program| ("custom".into(), program),
            ));
        }
        if builtin_animation(selected, 8).is_some() {
            Ok((selected.to_owned(), String::new()))
        } else {
            Ok((default.to_owned(), String::new()))
        }
    }

    pub fn animation_catalog(
        &self,
    ) -> io::Result<(
        Vec<sidepulse_core::AnimationChoice>,
        Vec<sidepulse_core::AgentAnimationState>,
    )> {
        use sidepulse_core::{AgentAnimationState, AnimationChoice};
        let mut choices = vec![AnimationChoice {
            id: "default".into(),
            name: "Use default".into(),
        }];
        choices.extend(BUILTIN_ANIMATIONS.iter().map(|(id, name)| AnimationChoice {
            id: (*id).into(),
            name: (*name).into(),
        }));
        let custom = self
            .document
            .get("custom_agent_animations")
            .and_then(Value::as_object);
        if let Some(custom) = custom {
            let mut named = custom
                .iter()
                .filter(|(id, value)| id.starts_with("custom:") && value.is_object())
                .map(|(id, value)| AnimationChoice {
                    id: id.clone(),
                    name: value
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or(id)
                        .into(),
                })
                .collect::<Vec<_>>();
            named.sort_by_key(|choice| choice.name.to_lowercase());
            choices.extend(named);
        }
        choices.push(AnimationChoice {
            id: "custom".into(),
            name: "Custom program for this status".into(),
        });
        let states = AgentMode::ALL
            .into_iter()
            .map(|mode| {
                let (style, program) = self.animation_for_mode(mode)?;
                let raw = self
                    .document
                    .get("agent_animations")
                    .and_then(|items| items.get(mode.key()))
                    .and_then(|value| value.get("style"))
                    .and_then(Value::as_str);
                let style = raw
                    .filter(|raw| choices.iter().any(|choice| choice.id == *raw))
                    .unwrap_or(&style)
                    .to_owned();
                Ok(AgentAnimationState {
                    mode,
                    style,
                    program,
                })
            })
            .collect::<io::Result<Vec<_>>>()?;
        Ok((choices, states))
    }

    pub fn set_agent_animation(
        &mut self,
        mode: AgentMode,
        style: &str,
        custom_program: Option<&str>,
    ) -> io::Result<()> {
        let valid = matches!(style, "default" | "custom")
            || builtin_animation(style, 8).is_some()
            || (style.starts_with("custom:")
                && self
                    .document
                    .get("custom_agent_animations")
                    .and_then(|items| items.get(style))
                    .is_some());
        if !valid {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "unknown animation style",
            ));
        }
        let program = if style == "custom" {
            Some(sidepulse_device::normalize_led_text(
                custom_program.ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "custom program is required")
                })?,
            ))
        } else {
            None
        };
        let mut updated = self.document.clone();
        let animations = updated
            .entry("agent_animations")
            .or_insert_with(|| json!({}))
            .as_object_mut()
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "agent_animations must be an object",
                )
            })?;
        let targets = if matches!(
            mode,
            AgentMode::Working | AgentMode::ToolRunning | AgentMode::LongTaskProgress
        ) {
            vec![
                AgentMode::Working,
                AgentMode::ToolRunning,
                AgentMode::LongTaskProgress,
            ]
        } else {
            vec![mode]
        };
        for target in targets {
            let setting = animations
                .entry(target.key())
                .or_insert_with(|| json!({}))
                .as_object_mut()
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "animation setting must be an object",
                    )
                })?;
            setting.insert("style".into(), json!(style));
            if let Some(program) = &program {
                setting.insert("custom_program".into(), json!(program));
            } else {
                setting.remove("custom_program");
            }
        }
        let candidate = Self {
            path: self.path.clone(),
            document: updated.clone(),
            original: self.original.clone(),
        };
        let (resolved_style, resolved_program) = candidate.animation_for_mode(mode)?;
        for count in [2, 8] {
            program_for_style(mode, count, 255, &resolved_style, &resolved_program)?;
        }
        self.original = Some(write_atomic(
            &self.path,
            &Value::Object(updated.clone()),
            self.original.as_deref(),
        )?);
        self.document = updated;
        Ok(())
    }

    pub fn set_brightness_for_device(&mut self, path: &Path, brightness: u8) -> io::Result<()> {
        let mut updated = self.document.clone();
        let devices = updated.entry("devices").or_insert_with(|| json!([]));
        let list = devices.as_array_mut().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "settings.devices must be an array",
            )
        })?;
        let target = target_from_device_path(path);
        let existing = list.iter_mut().find(|device| {
            device
                .get("path")
                .and_then(Value::as_str)
                .is_some_and(|value| target_from_device_path(Path::new(value)) == target)
        });
        if let Some(device) = existing {
            let object = device.as_object_mut().ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "settings device must be an object",
                )
            })?;
            object.insert("brightness".into(), json!(brightness));
        } else {
            let path_text = path.to_string_lossy().into_owned();
            let name = path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .into_owned();
            list.push(json!({
                "id": path_text,
                "name": name,
                "path": path_text,
                "led_display": "agent",
                "brightness": brightness,
            }));
        }
        self.original = Some(write_atomic(
            &self.path,
            &Value::Object(updated.clone()),
            self.original.as_deref(),
        )?);
        self.document = updated;
        Ok(())
    }

    pub fn set_display_for_device(
        &mut self,
        path: &Path,
        mode: &str,
        brightness: u8,
    ) -> io::Result<()> {
        if !matches!(mode, "agent" | "battery" | "custom") {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "unsupported display mode",
            ));
        }
        let mut updated = self.document.clone();
        let devices = updated.entry("devices").or_insert_with(|| json!([]));
        let list = devices.as_array_mut().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "settings.devices must be an array",
            )
        })?;
        let target = target_from_device_path(path);
        if let Some(device) = list.iter_mut().find(|device| {
            device
                .get("path")
                .and_then(Value::as_str)
                .is_some_and(|value| target_from_device_path(Path::new(value)) == target)
        }) {
            let object = device.as_object_mut().ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "settings device must be an object",
                )
            })?;
            object.insert("led_display".into(), json!(mode));
        } else {
            let path_text = path.to_string_lossy().into_owned();
            list.push(json!({
                "id": path_text,
                "name": path.file_name().unwrap_or_default().to_string_lossy(),
                "path": path_text,
                "led_display": mode,
                "brightness": brightness,
            }));
        }
        self.original = Some(write_atomic(
            &self.path,
            &Value::Object(updated.clone()),
            self.original.as_deref(),
        )?);
        self.document = updated;
        Ok(())
    }

    fn find_device(&self, path: &Path) -> Option<&Value> {
        let target = target_from_device_path(path);
        self.document
            .get("devices")?
            .as_array()?
            .iter()
            .find(|device| {
                device
                    .get("path")
                    .and_then(Value::as_str)
                    .is_some_and(|value| target_from_device_path(Path::new(value)) == target)
            })
    }
}

fn write_atomic(path: &Path, document: &Value, original: Option<&[u8]>) -> io::Result<Vec<u8>> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "refusing to replace a settings symlink",
            ));
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let current = match fs::read(path) {
        Ok(bytes) => Some(bytes),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(error),
    };
    if current.as_deref() != original {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "settings changed outside the Rust service",
        ));
    }
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let mut temporary = NamedTempFile::new_in(parent)?;
    if let Ok(metadata) = fs::metadata(path) {
        temporary
            .as_file()
            .set_permissions(metadata.permissions())?;
    }
    let mut bytes = serde_json::to_vec_pretty(document)?;
    bytes.push(b'\n');
    temporary.write_all(&bytes)?;
    temporary.flush()?;
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|error| error.error)?;
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn updates_device_brightness_without_losing_other_settings() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        let device = dir.path().join("SidePulse Dot");
        fs::write(&path, json!({
            "transcript_monitoring": {"codex": true},
            "devices": [{"id": "dot", "path": device.to_string_lossy(), "brightness": 40, "custom": "keep"}],
            "unknown": 9,
        }).to_string()).unwrap();
        let mut store = SettingsStore::load(&path).unwrap();
        assert_eq!(store.brightness_for_device(&device), 40);
        store.set_brightness_for_device(&device, 75).unwrap();
        let reloaded = SettingsStore::load(&path).unwrap();
        assert_eq!(reloaded.brightness_for_device(&device), 75);
        assert_eq!(reloaded.snapshot()["devices"][0]["custom"], "keep");
        assert_eq!(reloaded.snapshot()["transcript_monitoring"]["codex"], true);
        assert_eq!(reloaded.snapshot()["unknown"], 9);
    }

    #[test]
    fn updates_transcript_setting_without_losing_other_settings() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        fs::write(
            &path,
            r#"{"transcript_monitoring":{"codex":true},"custom":9}"#,
        )
        .unwrap();
        let mut store = SettingsStore::load(&path).unwrap();
        store.set_transcript_enabled("claude", true).unwrap();
        assert!(store.transcript_enabled("codex"));
        assert!(store.transcript_enabled("claude"));
        let reloaded = SettingsStore::load(&path).unwrap();
        assert_eq!(reloaded.snapshot()["custom"], 9);
        assert_eq!(reloaded.snapshot()["transcript_monitoring"]["claude"], true);
    }

    #[test]
    fn rejects_external_settings_edits() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        fs::write(&path, "{\"custom\":1}\n").unwrap();
        let mut store = SettingsStore::load(&path).unwrap();
        fs::write(&path, "{\"custom\":2}\n").unwrap();
        let error = store
            .set_brightness_for_device(Path::new("/test/device"), 10)
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::AlreadyExists);
        assert_eq!(fs::read_to_string(&path).unwrap(), "{\"custom\":2}\n");
    }

    #[test]
    fn reads_legacy_sleep_settings_for_service_policy() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        fs::write(
            &path,
            json!({
                "sleep_prevention_policy": "always",
                "closed_lid_system_override_enabled": true,
                "sleep_prevention": {"min_battery_percent": 27.5}
            })
            .to_string(),
        )
        .unwrap();
        let store = SettingsStore::load(&path).unwrap();
        assert_eq!(store.sleep_policy(), AwakePolicy::Always);
        assert_eq!(store.sleep_battery_threshold(), 27.5);
        assert!(store.closed_lid_system_override_enabled());
        let mut store = store;
        store.set_sleep_policy("never").unwrap();
        assert_eq!(store.sleep_policy(), AwakePolicy::Never);
        assert_eq!(store.snapshot()["sleep_prevention_policy"], "never");
        store
            .set_sleep_settings(&SleepSettingsPatch {
                min_battery_percent: Some(35.0),
                ..Default::default()
            })
            .unwrap();
        assert_eq!(store.sleep_battery_threshold(), 35.0);
        assert_eq!(store.sleep_policy(), AwakePolicy::Never);
        assert!(store.closed_lid_system_override_enabled());
        let before = fs::read(&path).unwrap();
        assert!(
            store
                .set_sleep_settings(&SleepSettingsPatch {
                    min_battery_percent: Some(101.0),
                    ..Default::default()
                })
                .is_err()
        );
        assert_eq!(fs::read(&path).unwrap(), before);
    }

    #[test]
    fn resolves_bundled_and_saved_custom_animation() {
        let directory = tempfile::tempdir().unwrap();
        let animations = directory.path().join("animations");
        fs::create_dir(&animations).unwrap();
        fs::write(animations.join("custom.LED"), "#123456 200ms ease\n").unwrap();
        let path = directory.path().join("settings.json");
        fs::write(
            &path,
            json!({
                "agent_animations": {
                    "working": {"style": "custom:demo"},
                    "waiting_for_input": {"style": "ember-attention"}
                },
                "custom_agent_animations": {
                    "custom:demo": {"name": "Demo", "file": "custom.LED"}
                }
            })
            .to_string(),
        )
        .unwrap();
        let store = SettingsStore::load(&path).unwrap();
        assert_eq!(
            store.animation_for_mode(AgentMode::Working).unwrap(),
            ("custom".into(), "#123456 200ms ease\n".into())
        );
        assert_eq!(
            store.animation_for_mode(AgentMode::ToolRunning).unwrap().0,
            "custom"
        );
        assert_eq!(
            store
                .animation_for_mode(AgentMode::WaitingForInput)
                .unwrap()
                .0,
            "ember-attention"
        );
        assert_eq!(
            store.animation_for_mode(AgentMode::Completed).unwrap().0,
            "cyan-complete"
        );
    }
}
