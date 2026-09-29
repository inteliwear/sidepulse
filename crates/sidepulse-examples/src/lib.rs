//! Headless example policies. Device output and audio capture stay outside UI.
use serde::{Deserialize, Serialize};
use std::{
    io,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
fn unit(value: f64) -> f64 {
    value.clamp(0.0, 1.0)
}
fn hex(rgb: [u8; 3], brightness: f64) -> String {
    let [r, g, b] =
        rgb.map(|channel| (f64::from(channel) * unit(brightness)).round_ties_even() as u8);
    format!("#{r:02X}{g:02X}{b:02X}")
}
pub fn gradient(index: usize, count: usize) -> [u8; 3] {
    let count = count.clamp(1, 8);
    let position = if count == 1 {
        0.0
    } else {
        index as f64 / (count - 1) as f64
    };
    if position <= 0.5 {
        [(255.0 * position / 0.5).round_ties_even() as u8, 255, 0]
    } else {
        [
            255,
            (255.0 * (1.0 - (position - 0.5) / 0.5)).round_ties_even() as u8,
            0,
        ]
    }
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct AudioOptions {
    pub idle_brightness: f64,
    pub max_brightness: f64,
    pub transition_ms: u32,
    pub noise_floor_db: f64,
    pub peak_db: f64,
    pub gain_db: f64,
    pub curve: f64,
    pub attack: f64,
    pub release: f64,
}
impl Default for AudioOptions {
    fn default() -> Self {
        Self {
            idle_brightness: 0.08,
            max_brightness: 1.0,
            transition_ms: 90,
            noise_floor_db: -54.0,
            peak_db: -8.0,
            gain_db: 0.0,
            curve: 0.72,
            attack: 0.045,
            release: 0.32,
        }
    }
}
impl AudioOptions {
    pub fn validate(&self) -> io::Result<()> {
        if [
            self.idle_brightness,
            self.max_brightness,
            self.noise_floor_db,
            self.peak_db,
            self.gain_db,
            self.curve,
            self.attack,
            self.release,
        ]
        .iter()
        .any(|value| !value.is_finite())
            || self.peak_db <= self.noise_floor_db
        {
            return Err(io::Error::other(
                "audio options must be finite; peak dB must exceed noise floor dB",
            ));
        }
        Ok(())
    }
    pub fn program(&self, level: f64, count: usize) -> io::Result<String> {
        self.validate()?;
        if !level.is_finite() {
            return Err(io::Error::other("audio level must be finite"));
        }
        let count = count.clamp(1, 8);
        let fill = unit(level) * count as f64;
        let idle = unit(self.idle_brightness);
        let peak = unit(self.max_brightness).max(idle);
        let segments: Vec<_> = (0..count)
            .map(|index| {
                let absolute = idle + (peak - idle) * unit(fill - index as f64);
                let brightness = if peak > 0.0 { absolute / peak } else { 0.0 };
                let token = format!("{index}:{}", hex(gradient(index, count), brightness));
                if self.transition_ms == 0 {
                    token
                } else {
                    format!("{token} {}ms cosine", self.transition_ms)
                }
            })
            .collect();
        let program = sidepulse_device::apply_brightness(
            &segments.join("; "),
            (peak * 255.0).round_ties_even() as u8,
        );
        sidepulse_device::validate_led_text(&program)?;
        Ok(program)
    }
    pub fn level(&self, rms: f64) -> f64 {
        unit(
            (dbfs(rms) + self.gain_db - self.noise_floor_db) / (self.peak_db - self.noise_floor_db),
        )
        .powf(self.curve.max(0.05))
    }
    pub fn smooth(&self, previous: f64, target: f64, elapsed: f64) -> f64 {
        let previous = unit(previous);
        let target = unit(target);
        let tau = if target > previous {
            self.attack
        } else {
            self.release
        };
        if tau <= 0.0 {
            return target;
        }
        previous + (target - previous) * (1.0 - (-elapsed.max(0.0) / tau).exp())
    }
}
pub fn dbfs(rms: f64) -> f64 {
    20.0 * rms.max(1e-9).log10()
}
pub fn score_program(
    left: i32,
    right: i32,
    count: usize,
    dim: f64,
    transition: u32,
) -> io::Result<String> {
    if !dim.is_finite() {
        return Err(io::Error::other("dim brightness must be finite"));
    }
    let count = count.clamp(2, 8);
    let half = count / 2;
    let left = left.max(0) as usize;
    let right = right.max(0) as usize;
    let segments: Vec<_> = (0..count)
        .map(|index| {
            let (rgb, lit) = if index < half {
                ([255, 0, 0], index < left.min(half))
            } else {
                ([0, 0, 255], index >= count - right.min(count - half))
            };
            let token = format!("{index}:{}", hex(rgb, if lit { 1.0 } else { dim }));
            if transition == 0 {
                token
            } else {
                format!("{token} {transition}ms cosine")
            }
        })
        .collect();
    let program = segments.join("; ");
    sidepulse_device::validate_led_text(&program)?;
    Ok(program)
}
pub fn celebration(left: bool) -> String {
    format!(
        "{} 500ms pulse\noff 100ms none\nrepeat 3",
        if left { "#FF0000" } else { "#0000FF" }
    )
}
#[derive(Debug, PartialEq, Eq)]
pub struct Score {
    pub left: i32,
    pub right: i32,
    pub state: String,
    pub detail: String,
}
pub fn find_match(data: &serde_json::Value) -> Option<Score> {
    let matches = |competitor: &serde_json::Value, wanted: &str| {
        ["displayName", "shortDisplayName", "name", "location"]
            .iter()
            .any(|key| {
                competitor
                    .get("team")
                    .and_then(|team| team.get(key))
                    .and_then(|value| value.as_str())
                    .is_some_and(|name| name.to_lowercase().contains(&wanted.to_lowercase()))
            })
    };
    let goals = |competitor: &serde_json::Value| match competitor.get("score") {
        None | Some(serde_json::Value::Null) => Some(0),
        Some(serde_json::Value::String(text)) if text.is_empty() => Some(0),
        Some(serde_json::Value::String(text)) => text.parse::<i32>().ok(),
        Some(value) => value.as_i64().and_then(|value| i32::try_from(value).ok()),
    };
    for event in data
        .get("events")
        .and_then(serde_json::Value::as_array)
        .into_iter()
        .flatten()
    {
        for competition in event
            .get("competitions")
            .and_then(serde_json::Value::as_array)
            .into_iter()
            .flatten()
        {
            let Some(competitors) = competition
                .get("competitors")
                .and_then(serde_json::Value::as_array)
            else {
                continue;
            };
            let left = competitors.iter().find(|team| matches(team, "Spain"));
            let right = competitors.iter().find(|team| matches(team, "Argentina"));
            if let (Some(left), Some(right)) = (left, right) {
                let status = &event["status"]["type"];
                return Some(Score {
                    left: goals(left)?,
                    right: goals(right)?,
                    state: status["state"].as_str().unwrap_or("pre").into(),
                    detail: status["shortDetail"]
                        .as_str()
                        .or_else(|| status["detail"].as_str())
                        .unwrap_or("")
                        .into(),
                });
            }
        }
    }
    None
}
pub fn target(
    device: Option<&Path>,
    file_name: &str,
    dry_run: bool,
) -> io::Result<Option<PathBuf>> {
    if file_name.is_empty() || file_name.contains(['/', '\\']) || matches!(file_name, "." | "..") {
        return Err(io::Error::other(
            "file name must be one ordinary path component",
        ));
    }
    if let Some(device) = device {
        return Ok(Some(
            if device.is_file()
                || device.file_name().is_some_and(|name| {
                    name.to_string_lossy()
                        .eq_ignore_ascii_case(sidepulse_device::DEFAULT_FILE_NAME)
                })
            {
                device.into()
            } else {
                device.join(file_name)
            },
        ));
    }
    if dry_run {
        return Ok(None);
    }
    let devices = sidepulse_device::discover_devices(&sidepulse_device::default_mount_roots());
    if devices.len() != 1 {
        return Err(io::Error::other(
            "provide --device when zero or multiple devices are mounted",
        ));
    }
    Ok(Some(devices[0].root.join(file_name)))
}
pub struct Output {
    writer: Option<sidepulse_device::DeviceOutput>,
    pub count: usize,
    pub dry_run: bool,
    off_on_exit: bool,
}
impl Output {
    pub fn new(
        device: Option<&Path>,
        file_name: &str,
        count: Option<usize>,
        dry_run: bool,
        off: bool,
    ) -> io::Result<Self> {
        let target = target(device, file_name, dry_run)?;
        let count = count.unwrap_or_else(|| {
            target
                .as_ref()
                .map_or(8, |target| sidepulse_device::led_count_for_target(target))
        });
        Ok(Self {
            writer: target.map(|target| sidepulse_device::DeviceOutput::with_target(&target, 255)),
            count,
            dry_run,
            off_on_exit: off,
        })
    }
    pub fn send(&mut self, program: &str) -> io::Result<()> {
        sidepulse_device::validate_led_text(program)?;
        if !self.dry_run
            && let Some(writer) = &mut self.writer
        {
            writer.sync_program(program)?;
        }
        Ok(())
    }
}
impl Drop for Output {
    fn drop(&mut self) {
        if self.off_on_exit
            && !self.dry_run
            && let Some(writer) = &mut self.writer
        {
            let _ = writer.sync_program("off");
        }
    }
}
pub fn stop_flag() -> io::Result<Arc<AtomicBool>> {
    let stop = Arc::new(AtomicBool::new(false));
    let signal = stop.clone();
    ctrlc::set_handler(move || signal.store(true, Ordering::Relaxed)).map_err(io::Error::other)?;
    Ok(stop)
}
pub fn wait(stop: &AtomicBool, duration: Duration) {
    let deadline = Instant::now() + duration;
    while !stop.load(Ordering::Relaxed) && Instant::now() < deadline {
        std::thread::sleep((deadline - Instant::now()).min(Duration::from_millis(25)));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn captured_python_audio_and_score_programs_match() {
        let cases: serde_json::Value =
            serde_json::from_str(include_str!("../fixtures/python-programs.json")).unwrap();
        for case in cases.as_array().unwrap() {
            let count = case["count"].as_u64().unwrap() as usize;
            let actual = if case["kind"] == "audio" {
                AudioOptions {
                    max_brightness: case["peak"].as_f64().unwrap(),
                    ..AudioOptions::default()
                }
                .program(case["level"].as_f64().unwrap(), count)
                .unwrap()
            } else {
                score_program(
                    case["left"].as_i64().unwrap() as i32,
                    case["right"].as_i64().unwrap() as i32,
                    count,
                    0.012,
                    250,
                )
                .unwrap()
            };
            assert_eq!(actual, case["program"].as_str().unwrap(), "{case}");
        }
    }
    #[test]
    fn db_mapping_attack_and_release_match_reference_values() {
        let options = AudioOptions {
            noise_floor_db: -60.0,
            peak_db: 0.0,
            curve: 1.0,
            attack: 0.05,
            release: 0.3,
            ..AudioOptions::default()
        };
        assert_eq!(options.level(0.0), 0.0);
        assert!((options.level(10.0_f64.powf(-30.0 / 20.0)) - 0.5).abs() < 1e-9);
        assert_eq!(options.level(1.0), 1.0);
        assert!(options.smooth(0.0, 1.0, 0.05) > 0.6);
        assert!(options.smooth(1.0, 0.0, 0.05) > 0.8);
        assert!(
            AudioOptions {
                peak_db: -60.0,
                ..options
            }
            .validate()
            .is_err()
        );
    }
    #[test]
    fn scoreboard_skips_other_events_and_preserves_status() {
        let data = serde_json::json!({"events":[{}, {"competitions":[{}, {"competitors":[{"team":{"displayName":"Spain"},"score":"2"},{"team":{"location":"Argentina"},"score":"1"}]}],"status":{"type":{"state":"post","shortDetail":"Full time"}}}]});
        assert_eq!(
            find_match(&data),
            Some(Score {
                left: 2,
                right: 1,
                state: "post".into(),
                detail: "Full time".into()
            })
        );
        assert!(find_match(&serde_json::json!({"events":[]})).is_none());
        sidepulse_device::validate_led_text(&celebration(true)).unwrap();
    }
    #[test]
    fn dry_run_keeps_mock_device_and_drop_off_is_explicit() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("LEDS.LED");
        std::fs::write(&target, "existing").unwrap();
        {
            let mut output =
                Output::new(Some(directory.path()), "LEDS.LED", Some(2), true, true).unwrap();
            output.send("#00FF00").unwrap();
        }
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "existing");
        {
            let mut output =
                Output::new(Some(directory.path()), "LEDS.LED", Some(2), false, true).unwrap();
            output.send("#00FF00").unwrap();
        }
        assert_eq!(std::fs::read_to_string(target).unwrap(), "off");
        assert!(Output::new(None, "../escape", None, true, false).is_err());
    }
}
