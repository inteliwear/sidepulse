//! Portable SidePulse LED program generation and mounted-device output.
//! The service owns `DeviceOutput`; UI clients never write device files.

use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use sidepulse_core::AgentMode;

pub mod battery;
pub mod battery_source;
pub mod virtual_led;

pub const DEFAULT_FILE_NAME: &str = "LEDS.LED";
pub const MAX_LED_BYTES: usize = 512;
pub const MAX_LED_LINES: usize = 20;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LedDisplayState {
    Idle,
    Working,
    Done,
    Ask,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceCandidate {
    pub root: PathBuf,
    pub target: PathBuf,
    pub reason: String,
}

pub fn display_state_for_mode(mode: AgentMode) -> LedDisplayState {
    match mode {
        AgentMode::WaitingForInput | AgentMode::BlockedError => LedDisplayState::Ask,
        AgentMode::Working | AgentMode::ToolRunning | AgentMode::LongTaskProgress => {
            LedDisplayState::Working
        }
        AgentMode::Completed => LedDisplayState::Done,
        _ => LedDisplayState::Idle,
    }
}

pub fn led_count_for_target(target: &Path) -> usize {
    let name = target
        .parent()
        .and_then(Path::file_name)
        .map(|name| normalized_name(&name.to_string_lossy()))
        .unwrap_or_default();
    if name.contains("sidepulsedot") || name.contains("pulsedot") {
        2
    } else {
        8
    }
}

pub fn program_for_mode(mode: AgentMode, led_count: usize, brightness: u8) -> String {
    let raw = match (display_state_for_mode(mode), led_count == 2) {
        (LedDisplayState::Idle, true) => include_str!("../resources/animations/idle-pulse-2.LED"),
        (LedDisplayState::Idle, false) => include_str!("../resources/animations/idle-pulse-8.LED"),
        (LedDisplayState::Working, true) => include_str!("../resources/animations/cyan-roll-2.LED"),
        (LedDisplayState::Working, false) => {
            include_str!("../resources/animations/cyan-roll-8.LED")
        }
        (LedDisplayState::Ask, _) => include_str!("../resources/animations/amber-pulse.LED"),
        (LedDisplayState::Done, _) => include_str!("../resources/animations/solid-green.LED"),
    };
    apply_brightness(raw.trim(), brightness)
}

pub fn normalize_led_text(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\\' {
            let replacement = match chars.peek() {
                Some('n') => Some('\n'),
                Some('r') => Some('\r'),
                Some('t') => Some('\t'),
                Some('\\') => Some('\\'),
                _ => None,
            };
            if let Some(replacement) = replacement {
                chars.next();
                out.push(replacement);
                continue;
            }
        }
        out.push(ch);
    }
    out
}

pub fn validate_led_text(text: &str) -> io::Result<()> {
    if text.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "LED program is empty",
        ));
    }
    if text.len() > MAX_LED_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "LED program exceeds 512 bytes",
        ));
    }
    if text.lines().count() > MAX_LED_LINES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "LED program exceeds 20 lines",
        ));
    }
    Ok(())
}

pub fn target_from_device_path(path: &Path) -> PathBuf {
    if path.file_name().is_some_and(|name| {
        name.to_string_lossy()
            .eq_ignore_ascii_case(DEFAULT_FILE_NAME)
    }) {
        path.to_path_buf()
    } else {
        path.join(DEFAULT_FILE_NAME)
    }
}

pub fn discover_devices(roots: &[PathBuf]) -> Vec<DeviceCandidate> {
    let mut candidates = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for root in roots {
        if root.is_dir() {
            let target = target_from_device_path(root);
            if target.is_file() && seen.insert(root.clone()) {
                candidates.push(DeviceCandidate {
                    root: root.clone(),
                    target,
                    reason: format!("contains {DEFAULT_FILE_NAME}"),
                });
            }
        }
        let Ok(entries) = fs::read_dir(root) else {
            continue;
        };
        let mut volumes: Vec<_> = entries.flatten().map(|entry| entry.path()).collect();
        volumes.sort_by_key(|path| {
            path.file_name()
                .map(|name| name.to_string_lossy().to_lowercase())
        });
        for volume in volumes {
            if !volume.is_dir() || !seen.insert(volume.clone()) {
                continue;
            }
            let name = volume.file_name().unwrap_or_default().to_string_lossy();
            if name == ".timemachine" || name == "Macintosh HD" {
                continue;
            }
            let target = target_from_device_path(&volume);
            let reason = if target.exists() {
                Some(format!("contains {DEFAULT_FILE_NAME}"))
            } else if is_device_name(&name) {
                Some("name matches device".to_owned())
            } else {
                None
            };
            if let Some(reason) = reason {
                candidates.push(DeviceCandidate {
                    root: volume,
                    target,
                    reason,
                });
            }
        }
    }
    candidates
}

pub fn default_mount_roots() -> Vec<PathBuf> {
    if let Some(configured) = std::env::var_os("SIDEPULSE_MOUNT_ROOTS") {
        return std::env::split_paths(&configured).collect();
    }
    #[cfg(target_os = "macos")]
    {
        vec![PathBuf::from("/Volumes")]
    }
    #[cfg(target_os = "linux")]
    {
        let user = std::env::var_os("HOME")
            .and_then(|home| {
                PathBuf::from(home)
                    .file_name()
                    .map(|name| name.to_os_string())
            })
            .unwrap_or_default();
        vec![
            PathBuf::from("/media").join(&user),
            PathBuf::from("/run/media").join(&user),
            PathBuf::from("/media"),
            PathBuf::from("/run/media"),
            PathBuf::from("/mnt"),
        ]
    }
    #[cfg(target_os = "windows")]
    {
        (b'D'..=b'Z')
            .map(|letter| PathBuf::from(format!("{}:\\", letter as char)))
            .collect()
    }
}

pub fn is_device_name(name: &str) -> bool {
    let name = normalized_name(name);
    ["sidepulsepro", "sidepulsedot", "pulsedot"]
        .iter()
        .any(|hint| name.contains(hint))
}

fn normalized_name(name: &str) -> String {
    name.chars()
        .filter(|ch| ch.is_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

pub fn apply_brightness(program: &str, brightness: u8) -> String {
    let mut found = false;
    let lines: Vec<_> = program
        .lines()
        .map(|line| {
            let trimmed = line.trim();
            let mut parts = trimmed.split_whitespace();
            if parts
                .next()
                .is_some_and(|key| key.eq_ignore_ascii_case("brightness"))
                && let Some(value) = parts.next().and_then(|value| value.parse::<u8>().ok())
                && parts.next().is_none()
            {
                found = true;
                let scaled =
                    ((u16::from(value) * u16::from(brightness)) as f64 / 255.0).round() as u8;
                return format!("brightness {scaled}");
            }
            line.to_owned()
        })
        .collect();
    let normalized = lines.join("\n");
    if found || brightness == 255 {
        normalized
    } else {
        format!("brightness {brightness}\n{normalized}")
    }
}

pub struct DeviceOutput {
    target: PathBuf,
    brightness: u8,
    last_program: Option<String>,
}

impl DeviceOutput {
    pub fn new(path: &Path, brightness: u8) -> Self {
        Self {
            target: target_from_device_path(path),
            brightness,
            last_program: None,
        }
    }

    pub fn target(&self) -> &Path {
        &self.target
    }

    pub fn brightness(&self) -> u8 {
        self.brightness
    }

    pub fn set_brightness(&mut self, brightness: u8) {
        self.brightness = brightness;
    }

    pub fn sync(&mut self, mode: AgentMode) -> io::Result<bool> {
        let program = program_for_mode(mode, led_count_for_target(&self.target), self.brightness);
        validate_led_text(&program)?;
        if self.last_program.as_deref() == Some(&program)
            && fs::read_to_string(&self.target).is_ok_and(|existing| existing == program)
        {
            return Ok(false);
        }
        let parent = self.target.parent().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "device target has no parent")
        })?;
        if !parent.is_dir() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "device is not mounted",
            ));
        }
        let is_new = !self.target.exists();
        let mut file = File::create(&self.target)?;
        file.write_all(program.as_bytes())?;
        let _ = file.sync_all();
        if is_new {
            let _ = File::open(parent).and_then(|directory| directory.sync_all());
        }
        self.last_program = Some(program);
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_programs_match_existing_two_led_device() {
        assert_eq!(
            program_for_mode(AgentMode::Working, 2, 255),
            "off 320ms cosine\n0:#00E5FF 760ms pulse 0ms; 1:#00E5FF 760ms pulse 260ms\nrepeat"
        );
        assert_eq!(
            program_for_mode(AgentMode::IdleReady, 2, 255),
            "off 2s\n0:#006060 1:#006060 2s ease\nrepeat"
        );
        assert_eq!(
            program_for_mode(AgentMode::Completed, 2, 64),
            "brightness 64\n#00FF66 320ms cosine"
        );
    }

    #[test]
    fn normalizes_and_rejects_out_of_bounds_programs() {
        assert_eq!(
            normalize_led_text(r"off\n#FF00FF pulse"),
            "off\n#FF00FF pulse"
        );
        assert!(validate_led_text(&"x".repeat(513)).is_err());
        assert!(validate_led_text(&["off"; 20].join("\n")).is_ok());
        assert!(validate_led_text(&["off"; 21].join("\n")).is_err());
    }

    #[test]
    fn writes_once_and_restores_external_changes() {
        let root = std::env::temp_dir().join(format!(
            "sp-device-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let device = root.join("SidePulseDot");
        fs::create_dir_all(&device).unwrap();
        let mut output = DeviceOutput::new(&device, 255);
        assert!(output.sync(AgentMode::Working).unwrap());
        assert!(!output.sync(AgentMode::Working).unwrap());
        fs::write(output.target(), "off").unwrap();
        assert!(output.sync(AgentMode::Working).unwrap());
        assert_eq!(
            fs::read_to_string(output.target()).unwrap(),
            program_for_mode(AgentMode::Working, 2, 255)
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn discovers_named_mount_without_touching_other_volumes() {
        let root = std::env::temp_dir().join(format!(
            "sp-discovery-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(root.join("SidePulseDot")).unwrap();
        fs::create_dir_all(root.join("Unrelated Disk")).unwrap();
        let found = discover_devices(std::slice::from_ref(&root));
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].target, root.join("SidePulseDot/LEDS.LED"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn discovers_device_when_mount_root_is_the_volume() {
        let parent = std::env::temp_dir().join(format!(
            "sp-drive-root-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let drive = parent.join("drive");
        fs::create_dir_all(&drive).unwrap();
        fs::write(drive.join("LEDS.LED"), "off\n").unwrap();
        let found = discover_devices(std::slice::from_ref(&drive));
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].root, drive);
        fs::remove_dir_all(parent).unwrap();
    }
}
