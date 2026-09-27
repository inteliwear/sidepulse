//! Development service with one authoritative monitor and a portable IPC API.

mod settings;

use std::fs::{self, File};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, mpsc};
use std::time::Duration;

use chrono::Utc;
use interprocess::local_socket::Stream;
use interprocess::local_socket::prelude::*;
use sidepulse_core::{
    ClientRequest, DeviceInfo, HookEvent, Monitor, MonitorSnapshot, PROTOCOL_VERSION, RequestKind,
    ServerMessage, ServerPayload, parse_log_line,
};
use sidepulse_device::animations::program_for_style;
use sidepulse_device::battery::{BatteryState, program_for_battery};
use sidepulse_device::battery_source::read_battery_state;
use sidepulse_device::led_count_for_target;
use sidepulse_device::{DeviceOutput, default_mount_roots, discover_devices};
use sidepulse_ipc::{read_message, write_message};
use sidepulse_sources::{SourceSpec, SourceTailer, load_recent_events, sources_from_environment};
use tempfile::NamedTempFile;

use settings::SettingsStore;

pub struct RuntimeSettings {
    pub document: serde_json::Value,
    pub active_device: Option<String>,
    pub brightness: Option<u8>,
    pub display_mode: Option<String>,
}

#[derive(Clone, Default)]
pub struct Service {
    monitor: Arc<Mutex<Monitor>>,
    subscribers: Arc<Mutex<Vec<mpsc::SyncSender<()>>>>,
    device: Arc<Mutex<Option<DeviceOutput>>>,
    latest_state_path: Arc<Option<PathBuf>>,
    settings: Arc<Mutex<Option<SettingsStore>>>,
}

impl Service {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_state_path(path: PathBuf) -> Self {
        Self {
            latest_state_path: Arc::new(Some(path)),
            ..Self::default()
        }
    }

    pub fn load_latest_state(&self) -> io::Result<usize> {
        let Some(path) = self.latest_state_path.as_deref() else {
            return Ok(0);
        };
        let text = match fs::read_to_string(path) {
            Ok(text) => text,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(0),
            Err(error) => return Err(error),
        };
        let Ok(document) = serde_json::from_str::<serde_json::Value>(&text) else {
            return Ok(0);
        };
        let statuses = document
            .get("statuses")
            .and_then(serde_json::Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|value| serde_json::from_value(value.clone()).ok());
        Ok(self
            .monitor
            .lock()
            .map_err(poisoned)?
            .restore_statuses(statuses))
    }

    pub fn persist_latest_state(&self) -> io::Result<()> {
        if let Some(path) = self.latest_state_path.as_deref() {
            let monitor = self.monitor.lock().map_err(poisoned)?;
            write_latest_state(path, &monitor)?;
        }
        Ok(())
    }

    pub fn configure_device(&self, path: &Path, brightness: u8) -> io::Result<()> {
        *self.device.lock().map_err(poisoned)? = Some(DeviceOutput::new(path, brightness));
        Ok(())
    }

    /// Keep the current mounted device while present, then select another
    /// discovered device or clear output after removal.
    pub fn auto_select_device(&self, roots: &[PathBuf]) -> io::Result<bool> {
        let candidates = discover_devices(roots);
        let mut device = self.device.lock().map_err(poisoned)?;
        let current = device.as_ref().map(|output| output.target().to_path_buf());
        let selected = candidates
            .iter()
            .find(|candidate| current.as_deref() == Some(candidate.target.as_path()))
            .or_else(|| candidates.first());
        if selected.map(|candidate| &candidate.target) == current.as_ref() {
            return Ok(false);
        }
        *device = if let Some(candidate) = selected {
            let brightness = self
                .settings
                .lock()
                .map_err(poisoned)?
                .as_ref()
                .map_or(255, |store| store.brightness_for_device(&candidate.root));
            Some(DeviceOutput::new(&candidate.root, brightness))
        } else {
            None
        };
        Ok(true)
    }

    pub fn available_devices(&self) -> io::Result<(Vec<DeviceInfo>, Option<String>)> {
        let devices = discover_devices(&default_mount_roots())
            .into_iter()
            .map(|candidate| DeviceInfo {
                root: candidate.root.to_string_lossy().into_owned(),
                target: candidate.target.to_string_lossy().into_owned(),
                reason: candidate.reason,
            })
            .collect();
        let active = self
            .device
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .map(|output| output.target().to_string_lossy().into_owned());
        Ok((devices, active))
    }

    pub fn select_device(&self, root: &str) -> io::Result<()> {
        let candidate = discover_devices(&default_mount_roots())
            .into_iter()
            .find(|candidate| candidate.root.to_string_lossy() == root)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "device is not mounted"))?;
        let brightness = self
            .settings
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .map_or(255, |store| store.brightness_for_device(&candidate.root));
        *self.device.lock().map_err(poisoned)? =
            Some(DeviceOutput::new(&candidate.root, brightness));
        Ok(())
    }

    pub fn configure_settings(&self, path: &Path) -> io::Result<()> {
        *self.settings.lock().map_err(poisoned)? = Some(SettingsStore::load(path)?);
        Ok(())
    }

    pub fn settings_snapshot(&self) -> io::Result<Option<RuntimeSettings>> {
        let device = self.device.lock().map_err(poisoned)?;
        let settings = self.settings.lock().map_err(poisoned)?;
        Ok(settings.as_ref().map(|store| RuntimeSettings {
            document: store.snapshot(),
            active_device: device
                .as_ref()
                .map(|output| output.target().to_string_lossy().into_owned()),
            brightness: device.as_ref().map(DeviceOutput::brightness),
            display_mode: device
                .as_ref()
                .map(|output| store.display_for_device(output.target()).to_owned()),
        }))
    }

    pub fn set_brightness(&self, brightness: u8) -> io::Result<()> {
        let mut device = self.device.lock().map_err(poisoned)?;
        let output = device
            .as_mut()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no device is configured"))?;
        let mut settings = self.settings.lock().map_err(poisoned)?;
        let store = settings.as_mut().ok_or_else(|| {
            io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
        })?;
        store.set_brightness_for_device(output.target(), brightness)?;
        output.set_brightness(brightness);
        Ok(())
    }

    pub fn set_display_mode(&self, mode: &str) -> io::Result<()> {
        let device = self.device.lock().map_err(poisoned)?;
        let output = device
            .as_ref()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no device is configured"))?;
        let mut settings = self.settings.lock().map_err(poisoned)?;
        let store = settings.as_mut().ok_or_else(|| {
            io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
        })?;
        store.set_display_for_device(output.target(), mode, output.brightness())
    }

    /// Only the service calls this; tray and CLI clients receive read-only snapshots.
    pub fn sync_device(&self) -> io::Result<Option<bool>> {
        let battery_display = {
            let device = self.device.lock().map_err(poisoned)?;
            let settings = self.settings.lock().map_err(poisoned)?;
            device.as_ref().is_some_and(|output| {
                settings
                    .as_ref()
                    .is_some_and(|store| store.display_for_device(output.target()) == "battery")
            })
        };
        let battery = if battery_display {
            read_battery_state()?
        } else {
            None
        };
        self.sync_device_with_battery(battery)
    }

    pub fn sync_device_with_battery(
        &self,
        battery: Option<BatteryState>,
    ) -> io::Result<Option<bool>> {
        let mode = self.snapshot()?.aggregate.mode;
        let mut device = self.device.lock().map_err(poisoned)?;
        let settings = self.settings.lock().map_err(poisoned)?;
        let Some(output) = device.as_mut() else {
            return Ok(None);
        };
        let battery_display = settings
            .as_ref()
            .is_some_and(|store| store.display_for_device(output.target()) == "battery");
        if battery_display && battery.is_none() {
            return Ok(Some(false));
        }
        if battery_display && let Some(mut state) = battery {
            if let Some(full_watts) = settings
                .as_ref()
                .and_then(SettingsStore::battery_full_charge_watts)
            {
                state.full_charge_watts = full_watts;
            }
            let program = program_for_battery(
                state,
                led_count_for_target(output.target()),
                360,
                output.brightness(),
            );
            return output.sync_program(&program).map(Some);
        }
        if let Some(store) = settings.as_ref() {
            let (style, custom) = store.animation_for_mode(mode)?;
            let program = program_for_style(
                mode,
                led_count_for_target(output.target()),
                output.brightness(),
                &style,
                &custom,
            )?;
            output.sync_program(&program).map(Some)
        } else {
            output.sync(mode).map(Some)
        }
    }

    /// Rebuild monitor state from the durable provider log after a restart.
    /// Malformed or unsupported rows are skipped, as in the live collector.
    pub fn replay_log(&self, provider: &str, path: &Path) -> io::Result<usize> {
        let reader = BufReader::new(File::open(path)?);
        let mut count = 0;
        for line in reader.lines() {
            let line = line?;
            if let Some(record) = parse_log_line(provider, &line) {
                self.monitor.lock().map_err(poisoned)?.ingest(&record);
                count += 1;
            }
        }
        Ok(count)
    }

    /// Replay the same bounded, time-ordered source set used by the CLI.
    pub fn replay_sources(&self, sources: &[SourceSpec], max_lines: usize) -> io::Result<usize> {
        let events = load_recent_events(sources, max_lines)?;
        let count = events.len();
        let mut monitor = self.monitor.lock().map_err(poisoned)?;
        for event in &events {
            monitor.ingest(event);
        }
        if let Some(path) = self.latest_state_path.as_deref() {
            write_latest_state(path, &monitor)?;
        }
        Ok(count)
    }

    pub fn ingest_record(&self, record: &HookEvent) -> io::Result<()> {
        {
            let mut monitor = self.monitor.lock().map_err(poisoned)?;
            monitor.ingest(record);
            if let Some(path) = self.latest_state_path.as_deref()
                && let Err(error) = write_latest_state(path, &monitor)
            {
                eprintln!("sidepulse-next-service: latest state: {error}");
            }
        }
        self.subscribers
            .lock()
            .map_err(poisoned)?
            .retain(|subscriber| {
                !matches!(
                    subscriber.try_send(()),
                    Err(mpsc::TrySendError::Disconnected(()))
                )
            });
        Ok(())
    }

    pub fn serve_connection(&self, mut stream: Stream) -> io::Result<()> {
        #[cfg(unix)]
        stream.set_recv_timeout(Some(Duration::from_secs(5)))?;
        let request: ClientRequest = read_message(&mut BufReader::new(&mut stream))?;
        if let Err(message) = request.validate() {
            return write_message(
                &mut stream,
                &ServerMessage {
                    version: PROTOCOL_VERSION,
                    request_id: Some(request.request_id),
                    payload: ServerPayload::Error {
                        code: "invalid_request".into(),
                        message: message.into(),
                    },
                },
            );
        }
        match request.kind {
            RequestKind::Snapshot => write_message(
                &mut stream,
                &ServerMessage {
                    version: PROTOCOL_VERSION,
                    request_id: Some(request.request_id),
                    payload: ServerPayload::Snapshot {
                        state: self.snapshot()?,
                    },
                },
            ),
            RequestKind::Devices => {
                let (devices, active_device) = self.available_devices()?;
                write_message(
                    &mut stream,
                    &ServerMessage {
                        version: PROTOCOL_VERSION,
                        request_id: Some(request.request_id),
                        payload: ServerPayload::Devices {
                            devices,
                            active_device,
                        },
                    },
                )
            }
            RequestKind::SelectDevice { root } => {
                let payload = match self.select_device(&root) {
                    Ok(()) => {
                        let (devices, active_device) = self.available_devices()?;
                        ServerPayload::Devices {
                            devices,
                            active_device,
                        }
                    }
                    Err(error) if error.kind() == io::ErrorKind::NotFound => ServerPayload::Error {
                        code: "device_unavailable".into(),
                        message: error.to_string(),
                    },
                    Err(error) => return Err(error),
                };
                write_message(
                    &mut stream,
                    &ServerMessage {
                        version: PROTOCOL_VERSION,
                        request_id: Some(request.request_id),
                        payload,
                    },
                )
            }
            RequestKind::Settings => {
                let payload = match self.settings_snapshot()? {
                    Some(settings) => ServerPayload::Settings {
                        settings: settings.document,
                        active_device: settings.active_device,
                        brightness: settings.brightness,
                        display_mode: settings.display_mode,
                    },
                    None => ServerPayload::Error {
                        code: "settings_unavailable".into(),
                        message: "the service was started without --settings".into(),
                    },
                };
                write_message(
                    &mut stream,
                    &ServerMessage {
                        version: PROTOCOL_VERSION,
                        request_id: Some(request.request_id),
                        payload,
                    },
                )
            }
            RequestKind::SetBrightness { brightness } => {
                let payload = match self
                    .set_brightness(brightness)
                    .and_then(|_| self.settings_snapshot())
                {
                    Ok(Some(settings)) => ServerPayload::Settings {
                        settings: settings.document,
                        active_device: settings.active_device,
                        brightness: settings.brightness,
                        display_mode: settings.display_mode,
                    },
                    Ok(None) => ServerPayload::Error {
                        code: "settings_unavailable".into(),
                        message: "the service was started without --settings".into(),
                    },
                    Err(error) if error.kind() == io::ErrorKind::NotFound => ServerPayload::Error {
                        code: "device_unavailable".into(),
                        message: error.to_string(),
                    },
                    Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                        ServerPayload::Error {
                            code: "settings_conflict".into(),
                            message: error.to_string(),
                        }
                    }
                    Err(error) => return Err(error),
                };
                write_message(
                    &mut stream,
                    &ServerMessage {
                        version: PROTOCOL_VERSION,
                        request_id: Some(request.request_id),
                        payload,
                    },
                )
            }
            RequestKind::SetDisplayMode { mode } => {
                let payload = match self
                    .set_display_mode(&mode)
                    .and_then(|_| self.settings_snapshot())
                {
                    Ok(Some(settings)) => ServerPayload::Settings {
                        settings: settings.document,
                        active_device: settings.active_device,
                        brightness: settings.brightness,
                        display_mode: settings.display_mode,
                    },
                    Ok(None) => ServerPayload::Error {
                        code: "settings_unavailable".into(),
                        message: "the service was started without --settings".into(),
                    },
                    Err(error) if error.kind() == io::ErrorKind::NotFound => ServerPayload::Error {
                        code: "device_unavailable".into(),
                        message: error.to_string(),
                    },
                    Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                        ServerPayload::Error {
                            code: "settings_conflict".into(),
                            message: error.to_string(),
                        }
                    }
                    Err(error) => return Err(error),
                };
                write_message(
                    &mut stream,
                    &ServerMessage {
                        version: PROTOCOL_VERSION,
                        request_id: Some(request.request_id),
                        payload,
                    },
                )
            }
            RequestKind::IngestHook { provider, line } => {
                let record = serde_json::to_string(&line)
                    .ok()
                    .and_then(|line| parse_log_line(&provider, &line));
                if let Some(record) = record {
                    self.ingest_record(&record)?;
                    write_message(
                        &mut stream,
                        &ServerMessage {
                            version: PROTOCOL_VERSION,
                            request_id: Some(request.request_id),
                            payload: ServerPayload::Ack,
                        },
                    )
                } else {
                    write_message(
                        &mut stream,
                        &ServerMessage {
                            version: PROTOCOL_VERSION,
                            request_id: Some(request.request_id),
                            payload: ServerPayload::Error {
                                code: "invalid_event".into(),
                                message: "the hook event could not be parsed".into(),
                            },
                        },
                    )
                }
            }
            RequestKind::Subscribe => {
                // One pending wakeup is sufficient: subscribers always fetch
                // the current monitor snapshot, so bursts cannot queue state.
                let (sender, receiver) = mpsc::sync_channel(1);
                self.subscribers.lock().map_err(poisoned)?.push(sender);
                write_message(
                    &mut stream,
                    &ServerMessage {
                        version: PROTOCOL_VERSION,
                        request_id: Some(request.request_id),
                        payload: ServerPayload::Snapshot {
                            state: self.snapshot()?,
                        },
                    },
                )?;
                while receiver.recv().is_ok() {
                    write_message(
                        &mut stream,
                        &ServerMessage {
                            version: PROTOCOL_VERSION,
                            request_id: None,
                            payload: ServerPayload::StateChanged {
                                state: self.snapshot()?,
                            },
                        },
                    )?;
                }
                Ok(())
            }
        }
    }

    pub fn snapshot(&self) -> io::Result<MonitorSnapshot> {
        Ok(self.monitor.lock().map_err(poisoned)?.snapshot(Utc::now()))
    }
}

fn poisoned<T>(_: std::sync::PoisonError<T>) -> io::Error {
    io::Error::other("service state lock poisoned")
}

fn write_latest_state(path: &Path, monitor: &Monitor) -> io::Result<()> {
    let now = Utc::now();
    let payload = serde_json::json!({
        "updated_at": now.to_rfc3339(),
        "statuses": monitor.stored_statuses().iter().map(|status| status.legacy_json(now)).collect::<Vec<_>>(),
    });
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let mut temporary = NamedTempFile::new_in(parent)?;
    serde_json::to_writer_pretty(&mut temporary, &payload)?;
    temporary.write_all(b"\n")?;
    temporary.flush()?;
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|error| error.error)?;
    Ok(())
}

pub fn run(endpoint: &str) -> io::Result<()> {
    run_with_logs(endpoint, &[])
}

pub fn run_with_logs(endpoint: &str, logs: &[(String, std::path::PathBuf)]) -> io::Result<()> {
    run_with_logs_and_device(endpoint, logs, None)
}

pub fn run_with_logs_and_device(
    endpoint: &str,
    logs: &[(String, std::path::PathBuf)],
    device: Option<(&Path, u8)>,
) -> io::Result<()> {
    run_with_options(
        endpoint,
        logs,
        device.map(|(path, brightness)| (path, Some(brightness))),
        None,
        None,
        false,
    )
}

pub fn run_with_options(
    endpoint: &str,
    logs: &[(String, PathBuf)],
    device: Option<(&Path, Option<u8>)>,
    latest_state_path: Option<&Path>,
    settings_path: Option<&Path>,
    auto_device: bool,
) -> io::Result<()> {
    let listener = sidepulse_ipc::bind(endpoint)?;
    let service = latest_state_path.map_or_else(Service::new, |path| {
        Service::with_state_path(path.to_path_buf())
    });
    if let Some(path) = settings_path {
        service.configure_settings(path)?;
    }
    service.load_latest_state()?;
    let mut source_overrides = logs.to_vec();
    if let Some(store) = service.settings.lock().map_err(poisoned)?.as_ref() {
        let home = std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        for (provider, relative) in [("codex", ".codex/sessions"), ("claude", ".claude/projects")] {
            let source_name = format!("{provider}-transcripts");
            if store.transcript_enabled(provider)
                && !source_overrides
                    .iter()
                    .any(|(name, _)| name == &source_name)
            {
                source_overrides.push((source_name, home.join(relative)));
            }
        }
    }
    let sources = sources_from_environment(&source_overrides);
    let mut tailer = SourceTailer::new(&sources)?;
    service.replay_sources(&sources, 5000)?;
    let recovery_service = service.clone();
    std::thread::spawn(move || {
        let mut last_error = None;
        loop {
            match tailer.poll().and_then(|events| {
                for event in &events {
                    recovery_service.ingest_record(event)?;
                }
                Ok(())
            }) {
                Ok(()) => last_error = None,
                Err(error) => {
                    let message = error.to_string();
                    if last_error.as_deref() != Some(message.as_str()) {
                        eprintln!("sidepulse-next-service: log recovery: {message}");
                    }
                    last_error = Some(message);
                }
            }
            std::thread::sleep(Duration::from_secs(1));
        }
    });
    if let Some((path, brightness)) = device {
        let brightness = match brightness {
            Some(brightness) => brightness,
            None => service
                .settings
                .lock()
                .map_err(poisoned)?
                .as_ref()
                .map_or(255, |store| store.brightness_for_device(path)),
        };
        service.configure_device(path, brightness)?;
    }
    if auto_device {
        let roots = default_mount_roots();
        service.auto_select_device(&roots)?;
        let selection_service = service.clone();
        std::thread::spawn(move || {
            loop {
                if let Err(error) = selection_service.auto_select_device(&roots) {
                    eprintln!("sidepulse-next-service: device discovery: {error}");
                }
                std::thread::sleep(Duration::from_secs(5));
            }
        });
    }
    if device.is_some() || auto_device {
        let output_service = service.clone();
        std::thread::spawn(move || {
            let mut last_error = None;
            loop {
                match output_service.sync_device() {
                    Ok(_) => last_error = None,
                    Err(error) => {
                        let message = error.to_string();
                        if last_error.as_deref() != Some(message.as_str()) {
                            eprintln!("sidepulse-next-service: device output: {message}");
                        }
                        last_error = Some(message);
                    }
                }
                std::thread::sleep(Duration::from_secs(1));
            }
        });
    }
    for connection in listener.incoming() {
        let stream = connection?;
        let service = service.clone();
        std::thread::spawn(move || {
            if let Err(error) = service.serve_connection(stream) {
                eprintln!("sidepulse-next-service: connection failed: {error}");
            }
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    use serde_json::json;

    #[test]
    fn automatic_device_selection_reconnects_after_mount_changes() {
        let directory = tempfile::tempdir().unwrap();
        let mounts = directory.path().join("mounts");
        std::fs::create_dir(&mounts).unwrap();
        let device = mounts.join("SidePulse Dot");
        let settings_path = directory.path().join("settings.json");
        std::fs::write(&settings_path, serde_json::json!({
            "devices": [{"id": "dot", "name": "SidePulse Dot", "path": device.to_string_lossy(), "brightness": 64}]
        }).to_string()).unwrap();
        let service = Service::new();
        service.configure_settings(&settings_path).unwrap();
        assert!(
            !service
                .auto_select_device(std::slice::from_ref(&mounts))
                .unwrap()
        );
        std::fs::create_dir(&device).unwrap();
        assert!(
            service
                .auto_select_device(std::slice::from_ref(&mounts))
                .unwrap()
        );
        assert_eq!(
            service.settings_snapshot().unwrap().unwrap().brightness,
            Some(64)
        );
        service.sync_device().unwrap();
        assert!(
            std::fs::read_to_string(device.join("LEDS.LED"))
                .unwrap()
                .starts_with("brightness 64\n")
        );
        assert!(
            !service
                .auto_select_device(std::slice::from_ref(&mounts))
                .unwrap()
        );
        std::fs::remove_dir_all(&device).unwrap();
        assert!(
            service
                .auto_select_device(std::slice::from_ref(&mounts))
                .unwrap()
        );
        assert_eq!(
            service.settings_snapshot().unwrap().unwrap().active_device,
            None
        );
        std::fs::create_dir(&device).unwrap();
        assert!(service.auto_select_device(&[mounts]).unwrap());
        assert_eq!(
            service.settings_snapshot().unwrap().unwrap().brightness,
            Some(64)
        );
    }

    #[cfg(unix)]
    #[test]
    fn service_owns_state_across_multiple_clients() {
        let directory = std::path::Path::new("/tmp").join(format!(
            "sidepulse-service-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&directory).unwrap();
        let endpoint = directory.join("service.sock");
        let endpoint = endpoint.to_str().unwrap().to_owned();
        let listener = sidepulse_ipc::bind(&endpoint).unwrap();
        let service = Service::new();
        let server = std::thread::spawn(move || {
            for _ in 0..2 {
                let stream = listener.accept().unwrap();
                service.serve_connection(stream).unwrap();
            }
        });
        let ingest = ClientRequest {
            version: PROTOCOL_VERSION,
            request_id: 1,
            kind: RequestKind::IngestHook {
                provider: "claude".into(),
                line: json!({"logged_at":"2026-09-26T12:00:00Z","hook_event_name":"PreToolUse","session_id":"a"}),
            },
        };
        let ack: ServerMessage =
            sidepulse_ipc::request(&endpoint, &ingest, Duration::from_secs(1)).unwrap();
        assert_eq!(ack.payload, ServerPayload::Ack);
        let snapshot: ServerMessage = sidepulse_ipc::request(
            &endpoint,
            &ClientRequest {
                version: PROTOCOL_VERSION,
                request_id: 2,
                kind: RequestKind::Snapshot,
            },
            Duration::from_secs(1),
        )
        .unwrap();
        let ServerPayload::Snapshot { state } = snapshot.payload else {
            panic!("expected snapshot")
        };
        assert_eq!(state.statuses.len() + state.stale_statuses.len(), 1);
        server.join().unwrap();
        std::fs::remove_dir(directory).unwrap();
    }

    #[test]
    fn replay_recovers_state_from_provider_log() {
        let directory = std::env::temp_dir().join(format!(
            "sidepulse-replay-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&directory).unwrap();
        let log = directory.join("claude.jsonl");
        std::fs::write(
            &log,
            concat!(
                "invalid json\n",
                "{\"logged_at\":\"2026-09-26T12:00:00Z\",\"hook_event_name\":\"PreToolUse\",\"session_id\":\"a\"}\n",
                "{\"logged_at\":\"2026-09-26T12:00:01Z\",\"hook_event_name\":\"Stop\",\"session_id\":\"a\"}\n"
            ),
        )
        .unwrap();
        let service = Service::new();
        assert_eq!(service.replay_log("claude", &log).unwrap(), 2);
        let snapshot = service.snapshot().unwrap();
        assert_eq!(snapshot.stale_statuses.len(), 1);
        assert_eq!(snapshot.stale_statuses[0].agent_id, "claude:session:a");
        assert_eq!(snapshot.stale_statuses[0].event_name, "Stop");
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn replay_sources_sorts_across_logs_before_restoring_state() {
        let directory = std::env::temp_dir().join(format!(
            "sidepulse-ordered-replay-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&directory).unwrap();
        let newer = directory.join("newer.jsonl");
        let older = directory.join("older.jsonl");
        let now = Utc::now();
        std::fs::write(
            &newer,
            serde_json::json!({
                "logged_at": now.to_rfc3339(),
                "hook_event_name": "Stop",
                "session_id": "same",
            })
            .to_string(),
        )
        .unwrap();
        std::fs::write(
            &older,
            serde_json::json!({
                "logged_at": (now - chrono::Duration::seconds(2)).to_rfc3339(),
                "hook_event_name": "PreToolUse",
                "session_id": "same",
            })
            .to_string(),
        )
        .unwrap();
        let service = Service::new();
        let sources = [
            SourceSpec {
                provider: "claude".into(),
                path: newer,
            },
            SourceSpec {
                provider: "claude".into(),
                path: older,
            },
        ];
        assert_eq!(service.replay_sources(&sources, 5000).unwrap(), 2);
        let snapshot = service.snapshot().unwrap();
        assert_eq!(snapshot.statuses[0].event_name, "Stop");
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn explicit_state_file_round_trips_legacy_status_schema() {
        let directory = std::env::temp_dir().join(format!(
            "sidepulse-state-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&directory).unwrap();
        let path = directory.join("latest.json");
        let service = Service::with_state_path(path.clone());
        let event = parse_log_line(
            "claude",
            &serde_json::json!({
                "logged_at": Utc::now().to_rfc3339(),
                "hook_event_name": "PreToolUse",
                "session_id": "persisted",
                "tool_name": "Shell",
            })
            .to_string(),
        )
        .unwrap();
        service.ingest_record(&event).unwrap();
        let document: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(document["statuses"][0]["mode"], "tool_running");
        assert_eq!(document["statuses"][0]["mode_label"], "Tool Running");
        assert_eq!(document["statuses"][0]["priority"], 3);
        assert!(document["updated_at"].is_string());
        let restarted = Service::with_state_path(path);
        assert_eq!(restarted.load_latest_state().unwrap(), 1);
        assert_eq!(
            restarted.snapshot().unwrap().statuses[0].agent_id,
            "claude:session:persisted"
        );
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn service_owns_device_output_without_tray_client() {
        let directory = std::env::temp_dir().join(format!(
            "sidepulse-output-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let device = directory.join("SidePulseDot");
        std::fs::create_dir_all(&device).unwrap();
        let service = Service::new();
        service.configure_device(&device, 255).unwrap();
        let line = serde_json::json!({
            "hook_event_name": "PreToolUse",
            "session_id": "a",
            "logged_at": Utc::now().to_rfc3339(),
        });
        let event = parse_log_line("claude", &line.to_string()).unwrap();
        service.monitor.lock().unwrap().ingest(&event);
        assert_eq!(service.sync_device().unwrap(), Some(true));
        assert!(
            std::fs::read_to_string(device.join("LEDS.LED"))
                .unwrap()
                .contains("#00E5FF")
        );
        assert_eq!(service.sync_device().unwrap(), Some(false));
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn service_uses_saved_battery_display_without_ui_writes() {
        let directory = tempfile::tempdir().unwrap();
        let device = directory.path().join("SidePulseDot");
        std::fs::create_dir(&device).unwrap();
        let settings = directory.path().join("settings.json");
        std::fs::write(
            &settings,
            serde_json::json!({
                "led_display": "agent",
                "battery_monitoring": {"full_charge_watts": 140.0},
                "devices": [{"path": device, "led_display": "battery", "brightness": 128}]
            })
            .to_string(),
        )
        .unwrap();
        let service = Service::new();
        service.configure_settings(&settings).unwrap();
        service.configure_device(&device, 128).unwrap();
        let battery = BatteryState {
            percent: 50,
            is_plugged: true,
            is_charging: true,
            adapter_watts: 70.0,
            ..Default::default()
        };
        assert_eq!(
            service.sync_device_with_battery(Some(battery)).unwrap(),
            Some(true)
        );
        let program = std::fs::read_to_string(device.join("LEDS.LED")).unwrap();
        assert!(program.starts_with("brightness 128\n"));
        assert!(program.contains("1:#80FFC8 790ms pulse"));
        assert_eq!(
            service.sync_device_with_battery(Some(battery)).unwrap(),
            Some(false)
        );
        assert_eq!(service.sync_device_with_battery(None).unwrap(), Some(false));
        assert_eq!(
            std::fs::read_to_string(device.join("LEDS.LED")).unwrap(),
            program
        );
    }

    #[test]
    fn service_uses_saved_agent_animation() {
        let directory = tempfile::tempdir().unwrap();
        let device = directory.path().join("SidePulseDot");
        std::fs::create_dir(&device).unwrap();
        let settings = directory.path().join("settings.json");
        std::fs::write(
            &settings,
            serde_json::json!({
                "agent_animations": {"idle_ready": {"style": "kitt-red"}}
            })
            .to_string(),
        )
        .unwrap();
        let service = Service::new();
        service.configure_settings(&settings).unwrap();
        service.configure_device(&device, 255).unwrap();
        assert_eq!(service.sync_device_with_battery(None).unwrap(), Some(true));
        let program = std::fs::read_to_string(device.join("LEDS.LED")).unwrap();
        assert_eq!(
            program,
            sidepulse_device::animations::program_for_style(
                sidepulse_core::AgentMode::IdleReady,
                2,
                255,
                "kitt-red",
                ""
            )
            .unwrap()
        );
    }
}
