//! Legacy settings document owned by the Rust service. Unknown fields are
//! retained so a development service can update one setting safely.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};
use sidepulse_core::AgentMode;
use sidepulse_device::animations::builtin_animation;
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

    pub fn transcript_enabled(&self, provider: &str) -> bool {
        self.document
            .get("transcript_monitoring")
            .and_then(|monitoring| monitoring.get(provider))
            .and_then(Value::as_bool)
            .unwrap_or(false)
    }

    pub fn animation_for_mode(&self, mode: AgentMode) -> io::Result<(String, String)> {
        let key = match mode {
            AgentMode::IdleReady => "idle_ready",
            AgentMode::Working => "working",
            AgentMode::ToolRunning => "tool_running",
            AgentMode::WaitingForInput => "waiting_for_input",
            AgentMode::LongTaskProgress => "long_task_progress",
            AgentMode::BlockedError => "blocked_error",
            AgentMode::Completed => "completed",
            AgentMode::Unknown => "unknown",
        };
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
        if !matches!(mode, "agent" | "battery") {
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
