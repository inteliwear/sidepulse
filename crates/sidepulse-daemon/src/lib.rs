//! Development service with one authoritative monitor and a portable IPC API.

mod power;
mod settings;
mod virtual_display;

use std::collections::{HashSet, VecDeque};
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};

use chrono::Utc;
use interprocess::local_socket::Stream;
use interprocess::local_socket::prelude::*;
use sidepulse_core::{
    AgentListSettingsPatch, BatterySettingsPatch, ClientRequest, DeviceInfo, HookEvent, Monitor,
    MonitorSnapshot, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload,
    SleepSettingsPatch, parse_log_line, parse_relay_message,
};
use sidepulse_device::animations::program_for_style;
use sidepulse_device::battery::{BatteryState, program_for_battery};
use sidepulse_device::battery_preview::BatteryPreview;
use sidepulse_device::battery_source::read_battery_state;
use sidepulse_device::led_count_for_target;
use sidepulse_device::{DeviceOutput, default_mount_roots, discover_devices};
use sidepulse_ipc::{read_message, write_message};
use sidepulse_relay::{load_config, publish_event, receive_once};
use sidepulse_sources::{SourceSpec, SourceTailer, load_recent_events, sources_from_environment};
use tempfile::NamedTempFile;

use settings::SettingsStore;

pub struct RuntimeSettings {
    pub document: serde_json::Value,
    pub active_device: Option<String>,
    pub brightness: Option<u8>,
    pub display_mode: Option<String>,
}

type RelayPublication = (String, serde_json::Value);
type RelayPublisher = mpsc::SyncSender<RelayPublication>;

#[derive(Clone, Default)]
pub struct Service {
    monitor: Arc<Mutex<Monitor>>,
    subscribers: Arc<Mutex<Vec<mpsc::SyncSender<()>>>>,
    device: Arc<Mutex<Option<DeviceOutput>>>,
    latest_state_path: Arc<Option<PathBuf>>,
    settings: Arc<Mutex<Option<SettingsStore>>>,
    seen_relay_events: Arc<Mutex<SeenRelayEvents>>,
    relay_publisher: Arc<Mutex<Option<RelayPublisher>>>,
    battery_preview: Arc<Mutex<BatteryPreview>>,
    virtual_output: Arc<Mutex<virtual_display::VirtualOutput>>,
}

#[derive(Default)]
struct SeenRelayEvents {
    ids: HashSet<String>,
    order: VecDeque<String>,
}

impl SeenRelayEvents {
    fn contains(&self, event_id: &str) -> bool {
        self.ids.contains(event_id)
    }

    fn insert(&mut self, event_id: String) {
        if !self.ids.insert(event_id.clone()) {
            return;
        }
        self.order.push_back(event_id);
        while self.order.len() > 2048 {
            if let Some(oldest) = self.order.pop_front() {
                self.ids.remove(&oldest);
            }
        }
    }
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
                label: candidate.label,
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

    pub fn set_transcript_monitoring(&self, provider: &str, enabled: bool) -> io::Result<()> {
        let mut settings = self.settings.lock().map_err(poisoned)?;
        let store = settings.as_mut().ok_or_else(|| {
            io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
        })?;
        store.set_transcript_enabled(provider, enabled)
    }

    pub fn set_battery_settings(&self, patch: &BatterySettingsPatch) -> io::Result<()> {
        let mut settings = self.settings.lock().map_err(poisoned)?;
        let store = settings.as_mut().ok_or_else(|| {
            io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
        })?;
        store.set_battery_settings(patch)
    }

    pub fn set_agent_list_settings(&self, patch: &AgentListSettingsPatch) -> io::Result<()> {
        {
            let mut settings = self.settings.lock().map_err(poisoned)?;
            let store = settings.as_mut().ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
            })?;
            store.set_agent_list_settings(patch)?;
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

    pub fn set_virtual_display(
        &self,
        patch: &sidepulse_core::VirtualDisplaySettingsPatch,
    ) -> io::Result<()> {
        self.settings
            .lock()
            .map_err(poisoned)?
            .as_mut()
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
            })?
            .set_virtual_display(patch)
    }

    pub fn virtual_display_frame(&self) -> io::Result<sidepulse_core::VirtualDisplayFrame> {
        let mode = self.snapshot()?.aggregate.mode;
        let settings = self.settings.lock().map_err(poisoned)?;
        let store = settings.as_ref();
        let (enabled, brightness, configured) = store.map_or(
            (false, 255, "agent"),
            SettingsStore::virtual_display_settings,
        );
        let preview = self.battery_preview.lock().map_err(poisoned)?;
        let display = preview.display(configured, Instant::now()).to_owned();
        let battery = preview.latest;
        drop(preview);
        let mut output = self.virtual_output.lock().map_err(poisoned)?;
        if !enabled || display == "custom" {
            output.clear();
            return Ok(sidepulse_core::VirtualDisplayFrame {
                enabled: false,
                display,
                pixels: vec![[0; 3]; 8],
            });
        }
        let program = if display == "battery"
            && let Some(mut battery) = battery
        {
            if let Some(watts) = store.and_then(SettingsStore::battery_full_charge_watts) {
                battery.full_charge_watts = watts;
            }
            program_for_battery(battery, 8, 360, brightness)
        } else if let Some(store) = store {
            let (style, custom) = store.animation_for_mode(mode)?;
            program_for_style(mode, 8, brightness, &style, &custom)?
        } else {
            sidepulse_device::program_for_mode(mode, 8, brightness)
        };
        Ok(sidepulse_core::VirtualDisplayFrame {
            enabled: true,
            display,
            pixels: output.pixels(&program)?,
        })
    }

    pub fn set_agent_animation(
        &self,
        mode: sidepulse_core::AgentMode,
        style: &str,
        custom_program: Option<&str>,
    ) -> io::Result<()> {
        let mut settings = self.settings.lock().map_err(poisoned)?;
        let store = settings.as_mut().ok_or_else(|| {
            io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
        })?;
        store.set_agent_animation(mode, style, custom_program)
    }

    pub fn set_sleep_settings(&self, patch: &SleepSettingsPatch) -> io::Result<()> {
        #[cfg(target_os = "macos")]
        {
            let mut settings = self.settings.lock().map_err(poisoned)?;
            let store = settings.as_mut().ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
            })?;
            store.set_sleep_settings(patch)
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = patch;
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "sleep prevention is available only on macOS",
            ))
        }
    }

    #[cfg(target_os = "macos")]
    pub fn set_sleep_policy(&self, policy: &str) -> io::Result<()> {
        let mut settings = self.settings.lock().map_err(poisoned)?;
        let store = settings.as_mut().ok_or_else(|| {
            io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
        })?;
        store.set_sleep_policy(policy)
    }

    pub fn ingest_relay_message(&self, text: &str) -> io::Result<bool> {
        let Some(event) = parse_relay_message(text) else {
            return Ok(false);
        };
        let Some(record) = parse_log_line(&event.provider, &event.line.to_string()) else {
            return Ok(false);
        };
        let mut seen = self.seen_relay_events.lock().map_err(poisoned)?;
        if !seen.contains(&event.event_id) {
            self.ingest_record(&record)?;
            seen.insert(event.event_id);
        }
        Ok(true)
    }

    /// Only the service calls this; tray and CLI clients receive read-only snapshots.
    pub fn sync_device(&self) -> io::Result<Option<bool>> {
        let battery = self.battery_preview.lock().map_err(poisoned)?.latest;
        self.render_device_with_battery_at(battery, Instant::now())
    }

    fn accept_battery(&self, battery: Option<BatteryState>, now: Instant) -> io::Result<()> {
        let (enabled, seconds) = self
            .settings
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .map_or((true, 7.0), SettingsStore::battery_preview_settings);
        self.battery_preview
            .lock()
            .map_err(poisoned)?
            .observe(battery, enabled, seconds, now);
        Ok(())
    }

    pub fn sync_device_with_battery(
        &self,
        battery: Option<BatteryState>,
    ) -> io::Result<Option<bool>> {
        self.sync_device_with_battery_at(battery, Instant::now())
    }

    fn sync_device_with_battery_at(
        &self,
        battery: Option<BatteryState>,
        now: Instant,
    ) -> io::Result<Option<bool>> {
        self.accept_battery(battery, now)?;
        self.render_device_with_battery_at(battery, now)
    }

    fn render_device_with_battery_at(
        &self,
        battery: Option<BatteryState>,
        now: Instant,
    ) -> io::Result<Option<bool>> {
        let mode = self.snapshot()?.aggregate.mode;
        let mut device = self.device.lock().map_err(poisoned)?;
        let settings = self.settings.lock().map_err(poisoned)?;
        let Some(output) = device.as_mut() else {
            return Ok(None);
        };
        let configured = settings
            .as_ref()
            .map_or("agent", |store| store.display_for_device(output.target()));
        let display = self
            .battery_preview
            .lock()
            .map_err(poisoned)?
            .display(configured, now)
            .to_owned();
        if display == "custom" {
            return Ok(Some(false));
        }
        let battery_display = display == "battery";
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
            RequestKind::Animations => {
                let payload = self
                    .settings
                    .lock()
                    .map_err(poisoned)?
                    .as_ref()
                    .map(|store| store.animation_catalog())
                    .transpose()?
                    .map_or_else(
                        || ServerPayload::Error {
                            code: "settings_unavailable".into(),
                            message: "no settings path is configured".into(),
                        },
                        |(choices, states)| ServerPayload::Animations { choices, states },
                    );
                write_message(
                    &mut stream,
                    &ServerMessage {
                        version: PROTOCOL_VERSION,
                        request_id: Some(request.request_id),
                        payload,
                    },
                )
            }
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
            RequestKind::VirtualDisplay => {
                let payload = match self.virtual_display_frame() {
                    Ok(frame) => ServerPayload::VirtualDisplay { frame },
                    Err(error) => ServerPayload::Error {
                        code: "virtual_display_error".into(),
                        message: error.to_string(),
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
            RequestKind::Power => {
                let payload = match power::observe() {
                    Ok(snapshot) => ServerPayload::Power { snapshot },
                    Err(error) if error.kind() == io::ErrorKind::Unsupported => {
                        ServerPayload::Error {
                            code: "unsupported_platform".into(),
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
            RequestKind::SetTranscriptMonitoring { provider, enabled } => {
                let payload = match self
                    .set_transcript_monitoring(&provider, enabled)
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
                        code: "settings_unavailable".into(),
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
            kind @ (RequestKind::SetAgentAnimation { .. }
            | RequestKind::SetVirtualDisplay { .. }
            | RequestKind::SetBatterySettings { .. }
            | RequestKind::SetAgentListSettings { .. }
            | RequestKind::SetSleepSettings { .. }) => {
                let result = match kind {
                    RequestKind::SetVirtualDisplay { patch } => self.set_virtual_display(&patch),
                    RequestKind::SetAgentAnimation {
                        mode,
                        style,
                        custom_program,
                    } => self.set_agent_animation(mode, &style, custom_program.as_deref()),
                    RequestKind::SetBatterySettings { patch } => self.set_battery_settings(&patch),
                    RequestKind::SetAgentListSettings { patch } => {
                        self.set_agent_list_settings(&patch)
                    }
                    RequestKind::SetSleepSettings { patch } => self.set_sleep_settings(&patch),
                    _ => unreachable!(),
                };
                let payload = match result.and_then(|_| self.settings_snapshot()) {
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
                    Err(error) => ServerPayload::Error {
                        code: match error.kind() {
                            io::ErrorKind::AlreadyExists => "settings_conflict",
                            io::ErrorKind::Unsupported => "unsupported_platform",
                            _ => "settings_update_failed",
                        }
                        .into(),
                        message: error.to_string(),
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
            RequestKind::SetSleepPolicy { policy } => {
                #[cfg(target_os = "macos")]
                let payload = match self
                    .set_sleep_policy(&policy)
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
                        code: "settings_unavailable".into(),
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
                #[cfg(not(target_os = "macos"))]
                let payload = {
                    let _ = policy;
                    ServerPayload::Error {
                        code: "unsupported_platform".into(),
                        message: "sleep prevention is available only on macOS".into(),
                    }
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
                let publish_line = line.clone();
                let record = serde_json::to_string(&line)
                    .ok()
                    .and_then(|line| parse_log_line(&provider, &line));
                if let Some(record) = record {
                    self.ingest_record(&record)?;
                    if let Some(sender) = self.relay_publisher.lock().map_err(poisoned)?.as_ref() {
                        let _ = sender.try_send((provider, publish_line));
                    }
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
            RequestKind::IngestRelay { message } => {
                let payload = if self.ingest_relay_message(&message.to_string())? {
                    ServerPayload::Ack
                } else {
                    ServerPayload::Error {
                        code: "invalid_relay_event".into(),
                        message: "the relay event could not be parsed".into(),
                    }
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
        self.snapshot_at(Utc::now())
    }

    fn snapshot_at(&self, now: chrono::DateTime<Utc>) -> io::Result<MonitorSnapshot> {
        let policy = self
            .settings
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .map_or_else(Default::default, SettingsStore::monitoring_policy);
        Ok(self
            .monitor
            .lock()
            .map_err(poisoned)?
            .snapshot_with_policy(now, policy))
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
        RunOptions {
            logs,
            device: device.map(|(path, brightness)| (path, Some(brightness))),
            latest_state_path: None,
            settings_path: None,
            auto_device: false,
            relay_config_path: None,
            power_control: false,
        },
    )
}

pub struct RunOptions<'a> {
    pub logs: &'a [(String, PathBuf)],
    pub device: Option<(&'a Path, Option<u8>)>,
    pub latest_state_path: Option<&'a Path>,
    pub settings_path: Option<&'a Path>,
    pub auto_device: bool,
    pub relay_config_path: Option<&'a Path>,
    pub power_control: bool,
}

fn source_overrides_with_settings(
    service: &Service,
    explicit: &[(String, PathBuf)],
    home: &Path,
) -> io::Result<Vec<(String, PathBuf)>> {
    let mut overrides = explicit.to_vec();
    if let Some(store) = service.settings.lock().map_err(poisoned)?.as_ref() {
        for (provider, relative) in [("codex", ".codex/sessions"), ("claude", ".claude/projects")] {
            let source_name = format!("{provider}-transcripts");
            if store.transcript_enabled(provider)
                && !overrides.iter().any(|(name, _)| name == &source_name)
            {
                overrides.push((source_name, home.join(relative)));
            }
        }
    }
    Ok(overrides)
}

pub fn run_with_options(endpoint: &str, options: RunOptions<'_>) -> io::Result<()> {
    let RunOptions {
        logs,
        device,
        latest_state_path,
        settings_path,
        auto_device,
        relay_config_path,
        power_control,
    } = options;
    #[cfg(not(target_os = "macos"))]
    if power_control {
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "power control is available only on macOS",
        ));
    }
    if power_control && settings_path.is_none() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "--power-control requires --settings",
        ));
    }
    let listener = sidepulse_ipc::bind(endpoint)?;
    let service = latest_state_path.map_or_else(Service::new, |path| {
        Service::with_state_path(path.to_path_buf())
    });
    if let Some(path) = settings_path {
        service.configure_settings(path)?;
    }
    #[cfg(target_os = "macos")]
    if power_control {
        use sidepulse_core::{BatteryPower, SleepInputs, battery_safeguard_active, plan_sleep};

        let power_service = service.clone();
        std::thread::spawn(move || {
            let mut controller = power::MacPowerController::new();
            let mut last_error = None;
            loop {
                let result = (|| -> io::Result<()> {
                    let (policy, threshold, allow_override) = {
                        let settings = power_service.settings.lock().map_err(poisoned)?;
                        let store = settings
                            .as_ref()
                            .ok_or_else(|| io::Error::other("power settings unavailable"))?;
                        (
                            store.sleep_policy(),
                            store.sleep_battery_threshold(),
                            store.closed_lid_system_override_enabled(),
                        )
                    };
                    let state = power_service.snapshot()?;
                    let battery = power_service
                        .battery_preview
                        .lock()
                        .map_err(poisoned)?
                        .latest
                        .map(|state| BatteryPower {
                            percent: f64::from(state.percent),
                            present: true,
                            plugged_in: state.is_plugged,
                        });
                    let observation = power::observe()?;
                    let plan = plan_sleep(SleepInputs {
                        policy,
                        agents_active: Some(state.aggregate.active_count > 0),
                        battery_safeguard_active: battery_safeguard_active(battery, threshold),
                        lid_closed: observation.lid_closed,
                        external_display_active: observation.external_display_active,
                    });
                    controller.sync(plan, allow_override)
                })();
                match result {
                    Ok(()) => last_error = None,
                    Err(error) => {
                        let message = error.to_string();
                        if last_error.as_deref() != Some(message.as_str()) {
                            eprintln!("sidepulse-next-service: power control: {message}");
                        }
                        last_error = Some(message);
                    }
                }
                std::thread::sleep(Duration::from_secs(1));
            }
        });
    }
    if let Some(path) = relay_config_path {
        let path = path.to_path_buf();
        let host = std::env::var("HOSTNAME")
            .or_else(|_| std::env::var("COMPUTERNAME"))
            .unwrap_or_else(|_| "Remote computer".into());
        let (sender, receiver) = mpsc::sync_channel(128);
        *service.relay_publisher.lock().map_err(poisoned)? = Some(sender);
        let publish_path = path.clone();
        let publish_host = host.clone();
        std::thread::spawn(move || {
            for (provider, line) in receiver {
                match load_config(&publish_path, &publish_host) {
                    Ok(config) if !config.outbound_channel.is_empty() => {
                        if let Err(error) = publish_event(&config, &provider, &line) {
                            eprintln!("sidepulse-next-service: relay send failed: {error}");
                        }
                    }
                    Ok(_) => {}
                    Err(error) => eprintln!("sidepulse-next-service: relay config: {error}"),
                }
            }
        });
        let receiver_service = service.clone();
        std::thread::spawn(move || {
            loop {
                match load_config(&path, &host) {
                    Ok(config) if !config.receiver_channel.is_empty() => {
                        if let Err(error) = receive_once(&config, |message| {
                            receiver_service.ingest_relay_message(&message)?;
                            Ok(())
                        }) {
                            eprintln!("sidepulse-next-service: relay receive failed: {error}");
                        }
                    }
                    Ok(_) => {}
                    Err(error) => eprintln!("sidepulse-next-service: relay config: {error}"),
                }
                std::thread::sleep(Duration::from_secs(1));
            }
        });
    }
    service.load_latest_state()?;
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let source_overrides = source_overrides_with_settings(&service, logs, &home)?;
    let sources = sources_from_environment(&source_overrides);
    let mut tailer = SourceTailer::new(&sources)?;
    service.replay_sources(&sources, 5000)?;
    let recovery_service = service.clone();
    let explicit_sources = logs.to_vec();
    std::thread::spawn(move || {
        let mut last_error = None;
        loop {
            let result = (|| -> io::Result<()> {
                let overrides =
                    source_overrides_with_settings(&recovery_service, &explicit_sources, &home)?;
                let desired = sources_from_environment(&overrides);
                let added = tailer.sync_transcripts(&desired)?;
                if !added.is_empty() {
                    recovery_service.replay_sources(&added, 5000)?;
                }
                let events = tailer.poll()?;
                for event in &events {
                    recovery_service.ingest_record(event)?;
                }
                Ok(())
            })();
            match result {
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
    if device.is_some() || auto_device || power_control {
        let battery_service = service.clone();
        std::thread::spawn(move || {
            let mut last_error = None;
            loop {
                let result = read_battery_state()
                    .and_then(|battery| battery_service.accept_battery(battery, Instant::now()));
                match result {
                    Ok(()) => last_error = None,
                    Err(error) => {
                        let message = error.to_string();
                        if last_error.as_deref() != Some(message.as_str()) {
                            eprintln!("sidepulse-next-service: battery: {message}");
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
    fn saved_monitor_timeout_changes_live_state_without_rebuilding_monitor() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("settings.json");
        fs::write(
            &path,
            r#"{"idle_timeout_seconds":45,"agent_list":{"custom":"keep"},"unknown":9}"#,
        )
        .unwrap();
        let service = Service::new();
        service.configure_settings(&path).unwrap();
        let event = parse_log_line("claude", r#"{"hook_event_name":"PreToolUse","session_id":"policy","logged_at":"2026-09-27T12:00:00Z"}"#).unwrap();
        service.ingest_record(&event).unwrap();
        let now = event.logged_at + chrono::Duration::minutes(10);
        assert_eq!(service.snapshot_at(now).unwrap().aggregate.active_count, 0);
        let (sent, received) = mpsc::sync_channel(1);
        service.subscribers.lock().unwrap().push(sent);
        service
            .set_agent_list_settings(&AgentListSettingsPatch {
                idle_timeout_seconds: Some(1200.0),
                recent_session_retention_seconds: Some(7200.0),
            })
            .unwrap();
        received.recv_timeout(Duration::from_millis(100)).unwrap();
        assert_eq!(service.snapshot_at(now).unwrap().aggregate.active_count, 1);
        let saved: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(saved["agent_list"]["custom"], "keep");
        assert_eq!(
            saved["agent_list"]["recent_session_retention_seconds"],
            7200.0
        );
        assert_eq!(saved["unknown"], 9);
        let before = fs::read(&path).unwrap();
        assert!(
            service
                .set_agent_list_settings(&AgentListSettingsPatch {
                    idle_timeout_seconds: Some(f64::INFINITY),
                    ..Default::default()
                })
                .is_err()
        );
        assert_eq!(fs::read(&path).unwrap(), before);
        assert_eq!(service.snapshot_at(now).unwrap().aggregate.active_count, 1);
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

    #[test]
    fn virtual_display_uses_service_programs_without_a_physical_device() {
        use sidepulse_core::{AgentMode, VirtualDisplaySettingsPatch};
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("settings.json");
        fs::write(&path, r#"{"unknown":7,"devices":[{"id":"virtual:status-bar","path":"virtual:status-bar","other":9}]}"#).unwrap();
        let service = Service::new();
        service.configure_settings(&path).unwrap();
        assert!(!service.virtual_display_frame().unwrap().enabled);
        service
            .set_agent_animation(AgentMode::IdleReady, "custom", Some("#FF0080"))
            .unwrap();
        service
            .set_virtual_display(&VirtualDisplaySettingsPatch {
                enabled: Some(true),
                ..Default::default()
            })
            .unwrap();
        let frame = service.virtual_display_frame().unwrap();
        assert!(frame.enabled);
        assert_eq!(frame.pixels, vec![[255, 0, 128]; 8]);
        service
            .set_virtual_display(&VirtualDisplaySettingsPatch {
                brightness: Some(0),
                ..Default::default()
            })
            .unwrap();
        assert_eq!(
            service.virtual_display_frame().unwrap().pixels,
            vec![[0; 3]; 8]
        );
        service
            .set_virtual_display(&VirtualDisplaySettingsPatch {
                display: Some("custom".into()),
                ..Default::default()
            })
            .unwrap();
        assert!(!service.virtual_display_frame().unwrap().enabled);
        let saved = service.settings_snapshot().unwrap().unwrap().document;
        assert_eq!(saved["unknown"], 7);
        assert_eq!(saved["devices"][0]["other"], 9);
        assert_eq!(saved["devices"].as_array().unwrap().len(), 1);
        assert_eq!(service.sync_device().unwrap(), None);
        assert!(!directory.path().join("LEDS.LED").exists());
        assert!(!Path::new("virtual:status-bar").exists());
    }

    #[test]
    fn animation_changes_apply_to_device_and_working_modes_without_losing_settings() {
        use sidepulse_core::AgentMode;
        let directory = tempfile::tempdir().unwrap();
        let device = directory.path().join("SidePulseDot");
        fs::create_dir(&device).unwrap();
        let path = directory.path().join("settings.json");
        fs::write(
            &path,
            r#"{"agent_animations":{"working":{"custom":"keep"}},"unknown":9}"#,
        )
        .unwrap();
        let service = Service::new();
        service.configure_settings(&path).unwrap();
        service.configure_device(&device, 255).unwrap();
        let event = parse_log_line("claude", &serde_json::json!({"hook_event_name":"PreToolUse","session_id":"animation","logged_at":Utc::now().to_rfc3339()}).to_string()).unwrap();
        service.ingest_record(&event).unwrap();
        service
            .set_agent_animation(AgentMode::Working, "kitt-red", None)
            .unwrap();
        service.sync_device().unwrap();
        let expected = program_for_style(AgentMode::ToolRunning, 2, 255, "kitt-red", "").unwrap();
        assert_eq!(
            fs::read_to_string(device.join("LEDS.LED")).unwrap(),
            expected
        );
        let saved = service.settings_snapshot().unwrap().unwrap().document;
        for mode in ["working", "tool_running", "long_task_progress"] {
            assert_eq!(saved["agent_animations"][mode]["style"], "kitt-red");
        }
        assert_eq!(saved["agent_animations"]["working"]["custom"], "keep");
        assert_eq!(saved["unknown"], 9);
        let before = fs::read(&path).unwrap();
        assert!(
            service
                .set_agent_animation(AgentMode::Working, "custom", Some("not an animation"))
                .is_err()
        );
        assert!(
            service
                .set_agent_animation(AgentMode::Working, "custom", Some(&"#123456\n".repeat(21)))
                .is_err()
        );
        assert!(
            service
                .set_agent_animation(AgentMode::Working, "missing-style", None)
                .is_err()
        );
        assert_eq!(fs::read(&path).unwrap(), before);
        service
            .set_agent_animation(
                AgentMode::ToolRunning,
                "custom",
                Some("#123456 200ms ease\nrepeat"),
            )
            .unwrap();
        service.sync_device().unwrap();
        assert!(
            fs::read_to_string(device.join("LEDS.LED"))
                .unwrap()
                .contains("#123456 200ms ease")
        );
        let settings = service.settings.lock().unwrap();
        let (choices, states) = settings.as_ref().unwrap().animation_catalog().unwrap();
        assert!(choices.iter().any(|choice| choice.id == "kitt-red"));
        assert!(
            states
                .iter()
                .filter(|state| matches!(
                    state.mode,
                    AgentMode::Working | AgentMode::ToolRunning | AgentMode::LongTaskProgress
                ))
                .all(|state| state.style == "custom" && state.program.contains("#123456"))
        );
    }

    #[test]
    fn service_previews_power_changes_then_resumes_agents_without_overwriting_manual_output() {
        let directory = tempfile::tempdir().unwrap();
        let device = directory.path().join("SidePulseDot");
        fs::create_dir(&device).unwrap();
        let path = directory.path().join("settings.json");
        fs::write(
            &path,
            r#"{"battery_monitoring":{"custom":"keep"},"unknown":9}"#,
        )
        .unwrap();
        let service = Service::new();
        service.configure_settings(&path).unwrap();
        service.configure_device(&device, 255).unwrap();
        let now = Instant::now();
        let battery = BatteryState {
            percent: 50,
            ..Default::default()
        };
        service
            .sync_device_with_battery_at(Some(battery), now)
            .unwrap();
        let agent_program = fs::read(device.join("LEDS.LED")).unwrap();
        let plugged = BatteryState {
            is_plugged: true,
            ..battery
        };
        assert_eq!(
            service
                .sync_device_with_battery_at(Some(plugged), now)
                .unwrap(),
            Some(true)
        );
        assert_ne!(fs::read(device.join("LEDS.LED")).unwrap(), agent_program);
        service
            .sync_device_with_battery_at(Some(plugged), now + Duration::from_secs(7))
            .unwrap();
        assert_eq!(fs::read(device.join("LEDS.LED")).unwrap(), agent_program);
        service.set_display_mode("custom").unwrap();
        fs::write(device.join("LEDS.LED"), "manual program").unwrap();
        assert_eq!(
            service
                .sync_device_with_battery_at(Some(battery), now + Duration::from_secs(8))
                .unwrap(),
            Some(false)
        );
        assert_eq!(
            fs::read_to_string(device.join("LEDS.LED")).unwrap(),
            "manual program"
        );
        service
            .set_battery_settings(&BatterySettingsPatch {
                full_charge_watts: Some(sidepulse_core::ChargerBaseline::Watts { watts: 140.0 }),
                show_on_power_change: Some(false),
                power_change_preview_seconds: Some(3.0),
                ..Default::default()
            })
            .unwrap();
        let saved: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(saved["unknown"], 9);
        assert_eq!(saved["battery_monitoring"]["custom"], "keep");
        assert_eq!(saved["battery_monitoring"]["full_charge_watts"], 140.0);
        assert_eq!(saved["devices"][0]["led_display"], "custom");
        let bytes = fs::read(&path).unwrap();
        assert!(
            service
                .set_battery_settings(&BatterySettingsPatch {
                    full_charge_watts: Some(sidepulse_core::ChargerBaseline::Watts {
                        watts: f64::NAN
                    }),
                    ..Default::default()
                })
                .is_err()
        );
        assert_eq!(fs::read(&path).unwrap(), bytes);
        service
            .set_battery_settings(&BatterySettingsPatch {
                full_charge_watts: Some(sidepulse_core::ChargerBaseline::Auto),
                ..Default::default()
            })
            .unwrap();
        assert!(service.settings_snapshot().unwrap().unwrap().document["battery_monitoring"]["full_charge_watts"].is_null());
    }
}
