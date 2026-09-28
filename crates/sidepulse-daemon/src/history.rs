//! Durable history owned by the service. Legacy JSONL stays readable by Python.

use std::collections::VecDeque;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use serde_json::Value;
use sidepulse_core::{AgentMode, HistoryPoint};

const MAX_POINTS: usize = 100_000;
const MAX_REPLY_POINTS: usize = 2000;
const MAX_REPLAY_BYTES: u64 = 128 * 1024 * 1024;

#[derive(Default)]
pub struct HistoryStore {
    path: Option<PathBuf>,
    points: VecDeque<HistoryPoint>,
}

impl HistoryStore {
    pub fn load(path: &Path) -> io::Result<Self> {
        let mut store = Self {
            path: Some(path.into()),
            ..Default::default()
        };
        let mut file = match File::open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(store),
            Err(error) => return Err(error),
        };
        let offset = file.metadata()?.len().saturating_sub(MAX_REPLAY_BYTES);
        file.seek(SeekFrom::Start(offset))?;
        let mut reader = BufReader::new(file.take(MAX_REPLAY_BYTES));
        if offset > 0 {
            reader.read_until(b'\n', &mut Vec::new())?;
        }
        for line in reader.lines() {
            if let Ok(value) = serde_json::from_str::<Value>(&line?)
                && let Some(point) = history_point(&value)
            {
                store.push(point);
            }
        }
        store
            .points
            .make_contiguous()
            .sort_by_key(|point| point.recorded_at);
        Ok(store)
    }

    fn push(&mut self, point: HistoryPoint) {
        self.points.push_back(point);
        while self.points.len() > MAX_POINTS {
            self.points.pop_front();
        }
    }

    pub fn append(&mut self, record: &Value) -> io::Result<()> {
        let Some(path) = &self.path else {
            return Ok(());
        };
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        if fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "refusing to append history through a symlink",
            ));
        }
        let mut file = OpenOptions::new().append(true).create(true).open(path)?;
        let mut bytes = serde_json::to_vec(record)?;
        bytes.push(b'\n');
        file.write_all(&bytes)?;
        if let Some(point) = history_point(record) {
            self.push(point);
        }
        Ok(())
    }

    pub fn snapshot(&self, seconds: u32) -> (Vec<HistoryPoint>, bool) {
        let Some(end) = self.points.iter().map(|point| point.recorded_at).max() else {
            return (Vec::new(), false);
        };
        let start = end - chrono::Duration::seconds(i64::from(seconds));
        let points: Vec<_> = self
            .points
            .iter()
            .filter(|point| point.recorded_at >= start)
            .collect();
        if points.len() <= MAX_REPLY_POINTS {
            return (points.into_iter().cloned().collect(), false);
        }
        // Bound the wire payload while retaining the start and latest observation.
        let last = points.len() - 1;
        let sampled = (0..MAX_REPLY_POINTS)
            .map(|index| points[index * last / (MAX_REPLY_POINTS - 1)].clone())
            .collect();
        (sampled, true)
    }
}

fn combined_bool(value: &Value, first: &str, second: &str) -> Option<bool> {
    match (
        value.get(first).and_then(Value::as_bool),
        value.get(second).and_then(Value::as_bool),
    ) {
        (Some(a), Some(b)) => Some(a || b),
        (Some(value), None) | (None, Some(value)) => Some(value),
        _ => None,
    }
}

fn history_point(value: &Value) -> Option<HistoryPoint> {
    let recorded_at = DateTime::parse_from_rfc3339(value.get("recorded_at")?.as_str()?)
        .ok()?
        .with_timezone(&Utc);
    let agent_status =
        serde_json::from_value(value.get("agent_status")?.clone()).unwrap_or(AgentMode::Unknown);
    Some(HistoryPoint {
        recorded_at,
        agent_status,
        display_status: value
            .get("display_status")
            .and_then(Value::as_str)
            .unwrap_or("")
            .into(),
        battery_level: value.get("battery_level").and_then(Value::as_f64),
        charger_power_watts: value.get("charger_power_watts").and_then(Value::as_f64),
        lid_closed: value.get("lid_closed").and_then(Value::as_bool),
        keep_awake_active: combined_bool(
            value,
            "sidepulse_keep_awake_active",
            "sidepulse_closed_lid_awake_active",
        ),
        keep_awake_requested: combined_bool(
            value,
            "sidepulse_keep_awake_requested",
            "sidepulse_closed_lid_awake_requested",
        ),
        mac_sleep_prevented: value.get("mac_sleep_prevented").and_then(Value::as_bool),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn legacy_rows_recover_and_filter_relative_to_last_observation() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("history.jsonl");
        fs::write(&path, "corrupt\n{\"recorded_at\":\"2026-01-01T00:00:00Z\",\"agent_status\":\"working\",\"battery_level\":50}\n{\"recorded_at\":\"2026-01-01T02:00:00Z\",\"agent_status\":\"completed\",\"battery_level\":40}\n").unwrap();
        let mut store = HistoryStore::load(&path).unwrap();
        let (points, sampled) = store.snapshot(3600);
        assert!(!sampled);
        assert_eq!(points.len(), 1);
        assert_eq!(points[0].battery_level, Some(40.0));
        store.append(&json!({"recorded_at":"2026-01-01T03:00:00Z","agent_status":"idle_ready","unknown":"preserved"})).unwrap();
        assert_eq!(
            HistoryStore::load(&path).unwrap().snapshot(172800).0.len(),
            3
        );
        assert!(fs::read_to_string(path).unwrap().contains("preserved"));
    }

    #[test]
    fn reply_is_bounded_and_retains_the_first_and_last_points() {
        let mut store = HistoryStore::default();
        let start = Utc::now();
        for index in 0..5000 {
            store.push(HistoryPoint {
                recorded_at: start + chrono::Duration::seconds(index),
                agent_status: AgentMode::Working,
                display_status: "Working".into(),
                battery_level: Some(50.0),
                charger_power_watts: None,
                lid_closed: None,
                keep_awake_active: None,
                keep_awake_requested: None,
                mac_sleep_prevented: None,
            });
        }
        let (points, sampled) = store.snapshot(21600);
        assert!(sampled);
        assert_eq!(points.len(), 2000);
        assert_eq!(points[0].recorded_at, start);
        assert_eq!(
            points.last().unwrap().recorded_at,
            start + chrono::Duration::seconds(4999)
        );
        assert!(serde_json::to_vec(&points).unwrap().len() < 1024 * 1024);
    }
}
