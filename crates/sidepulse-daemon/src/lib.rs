//! Development service with one authoritative monitor and a portable IPC API.

mod history;
mod phones;
mod power;
mod relay_service;
mod settings;
mod virtual_display;

use std::collections::{BTreeMap, HashSet, VecDeque};
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
use sidepulse_device::battery_diagnostics::{BatterySnapshot, read_battery_snapshot};
use sidepulse_device::battery_preview::BatteryPreview;
use sidepulse_device::led_count_for_target;
use sidepulse_device::{DeviceOutput, default_mount_roots, discover_devices};
use sidepulse_ipc::{read_message, write_message};
use sidepulse_relay::{publish_event, receive_once_while};
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
    shutdown: Arc<std::sync::atomic::AtomicBool>,
    monitor: Arc<Mutex<Monitor>>,
    subscribers: Arc<Mutex<Vec<mpsc::SyncSender<()>>>>,
    device: Arc<Mutex<Option<DeviceOutput>>>,
    latest_state_path: Arc<Option<PathBuf>>,
    settings: Arc<Mutex<Option<SettingsStore>>>,
    seen_relay_events: Arc<Mutex<SeenRelayEvents>>,
    relay_publisher: Arc<Mutex<Option<RelayPublisher>>>,
    battery_preview: Arc<Mutex<BatteryPreview>>,
    virtual_output: Arc<Mutex<virtual_display::VirtualOutput>>,
    history: Arc<Mutex<history::HistoryStore>>,
    battery_diagnostics: Arc<Mutex<Option<BatterySnapshot>>>,
    history_power: Arc<Mutex<HistoryPowerState>>,
    power_observation: Arc<Mutex<Option<sidepulse_core::PowerSnapshot>>>,
    lid_output: Arc<Mutex<sidepulse_core::LidOutputPolicy>>,
    output_started: Arc<OutputClock>,
    relay_config_store: Arc<Mutex<Option<sidepulse_relay::RelayConfigStore>>>,
    relay_health: Arc<Mutex<relay_service::RelayHealth>>,
    relay_generation: Arc<std::sync::atomic::AtomicU64>,
    phone_links: Arc<Mutex<Option<sidepulse_links::PhoneStore>>>,
    phone_pairing: Arc<Mutex<Option<phones::PairingRuntime>>>,
    delivery_jobs: Arc<Mutex<BTreeMap<String, sidepulse_core::DeliveryJobView>>>,
    phone_output: Arc<Mutex<phones::PhoneOutputState>>,
    phone_send_gate: Arc<Mutex<()>>,
    power_control_status: Arc<Mutex<sidepulse_core::PowerControlStatus>>,
    power_retry_generation: Arc<std::sync::atomic::AtomicU64>,
}

struct OutputClock(Instant);
impl Default for OutputClock {
    fn default() -> Self {
        Self(Instant::now())
    }
}

#[derive(Default)]
struct HistoryPowerState {
    requested: bool,
    active: bool,
    closed_lid_requested: bool,
    closed_lid_active: bool,
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

    pub fn configure_history(&self, path: &Path) -> io::Result<()> {
        *self.history.lock().map_err(poisoned)? = history::HistoryStore::load(path)?;
        Ok(())
    }

    pub fn animation_library(&self) -> io::Result<sidepulse_core::AnimationLibrary> {
        self.settings
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
            })?
            .animation_library()
    }

    pub fn export_animation_profile(
        &self,
        id: Option<&str>,
    ) -> io::Result<sidepulse_core::AnimationProfileDocument> {
        self.settings
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
            })?
            .export_animation_profile(id)
    }

    pub fn edit_animation_library(
        &self,
        edit: &sidepulse_core::AnimationLibraryEdit,
    ) -> io::Result<()> {
        self.settings
            .lock()
            .map_err(poisoned)?
            .as_mut()
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
            })?
            .edit_animation_library(edit)?;
        Ok(())
    }

    pub fn set_animation_state(
        &self,
        state: &str,
        style: &str,
        custom_program: Option<&str>,
    ) -> io::Result<()> {
        self.settings
            .lock()
            .map_err(poisoned)?
            .as_mut()
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
            })?
            .set_animation_state(state, style, custom_program)?;
        Ok(())
    }

    pub fn session_targets(
        &self,
        agent_id: &str,
        requested: Option<sidepulse_core::SessionAction>,
    ) -> io::Result<ServerPayload> {
        let snapshot = self.snapshot()?;
        let status = snapshot
            .statuses
            .iter()
            .find(|status| status.agent_id == agent_id)
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "session is no longer available")
            })?;
        let home = std::env::var("HOME")
            .or_else(|_| std::env::var("USERPROFILE"))
            .unwrap_or_else(|_| ".".into());
        let options = sidepulse_core::session_open_options(status, &home);
        let settings = self
            .settings
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .map_or_else(|| serde_json::json!({}), SettingsStore::snapshot);
        if requested.is_some_and(|action| !options.iter().any(|option| option.action == action)) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "this session cannot be opened with the selected action",
            ));
        }
        let selected = requested
            .or_else(|| sidepulse_core::preferred_session_action(status, &settings, &options));
        Ok(ServerPayload::SessionTargets {
            options,
            selected,
            terminal: settings
                .get("session_terminal_app")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("terminal")
                .into(),
            custom_terminal_path: settings
                .get("custom_terminal_path")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("")
                .into(),
        })
    }

    pub fn set_session_open_preference(
        &self,
        provider: &str,
        origin: Option<&str>,
        action: sidepulse_core::SessionAction,
    ) -> io::Result<()> {
        self.settings
            .lock()
            .map_err(poisoned)?
            .as_mut()
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
            })?
            .set_session_open_preference(provider, origin, action)
    }

    pub fn set_session_terminal(
        &self,
        terminal: &str,
        custom_path: Option<&str>,
    ) -> io::Result<()> {
        self.settings
            .lock()
            .map_err(poisoned)?
            .as_mut()
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
            })?
            .set_session_terminal(terminal, custom_path)
    }

    pub fn set_history_timeframe(&self, seconds: u32) -> io::Result<()> {
        self.settings
            .lock()
            .map_err(poisoned)?
            .as_mut()
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
            })?
            .set_history_timeframe(seconds)
    }

    pub fn history_snapshot(&self) -> io::Result<ServerPayload> {
        let seconds = self
            .settings
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .map_or(43200, SettingsStore::history_timeframe);
        let (points, sampled) = self.history.lock().map_err(poisoned)?.snapshot(seconds);
        Ok(ServerPayload::History {
            points,
            timeframe_seconds: seconds,
            sampled,
        })
    }

    pub fn record_history(
        &self,
        observation: Option<&sidepulse_core::PowerSnapshot>,
    ) -> io::Result<()> {
        use serde_json::json;
        let state = self.snapshot()?;
        let battery = self.battery_diagnostics.lock().map_err(poisoned)?.clone();
        let settings = self.settings.lock().map_err(poisoned)?;
        let document = settings
            .as_ref()
            .map_or_else(|| json!({}), SettingsStore::snapshot);
        let policy = document
            .get("sleep_prevention_policy")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("agents");
        let threshold = document
            .get("sleep_prevention")
            .and_then(|value| value.get("min_battery_percent"))
            .or_else(|| document.get("sleep_prevention_min_battery_percent"))
            .and_then(serde_json::Value::as_f64)
            .unwrap_or(20.0)
            .clamp(0.0, 100.0);
        let safeguard = sidepulse_core::battery_safeguard_active(
            battery
                .as_ref()
                .map(|battery| sidepulse_core::BatteryPower {
                    percent: f64::from(battery.percent),
                    present: battery.battery_present,
                    plugged_in: battery.is_plugged,
                }),
            threshold,
        );
        let power = self.history_power.lock().map_err(poisoned)?;
        let sleep = observation.map(|snapshot| &snapshot.mac_sleep);
        let lid = observation.and_then(|snapshot| snapshot.lid_closed);
        let round = |value: f64| (value * 100.0).round() / 100.0;
        let rich = battery.as_ref().filter(|_| cfg!(target_os = "macos"));
        let record = json!({
            "recorded_at": Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
            "agent_status": state.aggregate.mode,
            "display_status": match sidepulse_device::display_state_for_mode(state.aggregate.mode) {
                sidepulse_device::LedDisplayState::Idle => "Idle",
                sidepulse_device::LedDisplayState::Working => "Working",
                sidepulse_device::LedDisplayState::Done => "Done",
                sidepulse_device::LedDisplayState::Ask => "Ask",
            },
            "battery_level": battery.as_ref().map(|battery| battery.percent),
            "battery_charging": battery.as_ref().map(|battery| battery.is_charging),
            "battery_charged": battery.as_ref().map(|battery| battery.is_charged),
            "battery_present": battery.as_ref().map(|battery| battery.battery_present),
            "battery_power_watts": rich.map(|battery| round(battery.battery_watts)),
            "charger_connected": battery.as_ref().map(|battery| battery.is_plugged),
            "adapter_connected": rich.map(|battery| battery.adapter_connected),
            "charger_power_watts": rich.map(|battery| round(battery.adapter_power())),
            "adapter_watts": rich.map(|battery| battery.adapter_watts),
            "adapter_voltage": rich.map(|battery| round(battery.adapter_voltage)),
            "adapter_current": rich.map(|battery| round(battery.adapter_current)),
            "adapter_name": rich.map_or("", |battery| battery.adapter_name.as_str()),
            "adapter_manufacturer": rich.map_or("", |battery| battery.adapter_manufacturer.as_str()),
            "adapter_model": rich.map_or("", |battery| battery.adapter_model.as_str()),
            "lid_closed": lid,
            "lid_status": match lid { Some(true) => "closed", Some(false) => "open", None => "unknown" },
            "sidepulse_keep_awake_requested": power.requested,
            "sidepulse_keep_awake_active": power.active,
            "sleep_prevention_policy": policy,
            "sleep_prevention_battery_safeguard_active": safeguard,
            "sleep_prevention_min_battery_percent": threshold,
            "sidepulse_closed_lid_awake_requested": power.closed_lid_requested,
            "sidepulse_closed_lid_awake_active": power.closed_lid_active,
            "mac_sleep_prevented": sleep.and_then(|sleep| sleep.sleep_prevented()),
            "mac_sleep_disabled": sleep.and_then(|sleep| sleep.sleep_disabled),
            "mac_prevent_system_sleep": sleep.and_then(|sleep| sleep.prevent_system_sleep),
            "mac_prevent_user_idle_system_sleep": sleep.and_then(|sleep| sleep.prevent_user_idle_system_sleep),
            "mac_prevent_user_idle_display_sleep": sleep.and_then(|sleep| sleep.prevent_user_idle_display_sleep),
            "mac_user_is_active": sleep.and_then(|sleep| sleep.user_is_active),
            "mac_sleep_status": match sleep.and_then(|sleep| sleep.sleep_prevented()) { Some(true) => "prevented", Some(false) => "allowed", None => "unknown" },
            "mac_sleep_error": "",
        });
        drop(power);
        drop(settings);
        self.history.lock().map_err(poisoned)?.append(&record)
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
    pub fn set_lid_animation_timing(
        &self,
        open_seconds: Option<f64>,
        close_seconds: Option<f64>,
    ) -> io::Result<()> {
        self.settings
            .lock()
            .map_err(poisoned)?
            .as_mut()
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
            })?
            .set_lid_animation_timing(open_seconds, close_seconds)
    }

    pub fn observe_power(&self, observation: &sidepulse_core::PowerSnapshot) -> io::Result<()> {
        let mut cached = self.power_observation.lock().map_err(poisoned)?;
        let mut observation = observation.clone();
        if observation.lid_closed.is_none() {
            observation.lid_closed = cached.as_ref().and_then(|cached| cached.lid_closed);
        }
        *cached = Some(observation);
        Ok(())
    }

    pub fn update_battery_snapshot(&self, snapshot: BatterySnapshot) -> io::Result<()> {
        self.accept_battery(snapshot.led_state(), Instant::now())?;
        *self.battery_diagnostics.lock().map_err(poisoned)? = Some(snapshot);
        Ok(())
    }
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
        let snapshot = self.snapshot()?;
        let mode = snapshot.aggregate.mode;
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
        let observation = self
            .power_observation
            .lock()
            .map_err(poisoned)?
            .clone()
            .unwrap_or_default();
        let store = settings.as_ref();
        let policy = store.map_or(
            sidepulse_core::AwakePolicy::Agents,
            SettingsStore::sleep_policy,
        );
        let threshold = store.map_or(20.0, SettingsStore::sleep_battery_threshold);
        let inputs = sidepulse_core::SleepInputs {
            policy,
            agents_active: Some(snapshot.aggregate.active_count > 0),
            battery_safeguard_active: sidepulse_core::battery_safeguard_active(
                battery.map(|battery| sidepulse_core::BatteryPower {
                    percent: f64::from(battery.percent),
                    present: true,
                    plugged_in: battery.is_plugged,
                }),
                threshold,
            ),
            lid_closed: observation.lid_closed,
            external_display_active: observation.external_display_active,
        };
        let now_ms = now
            .saturating_duration_since(self.output_started.0)
            .as_millis() as u64;
        let action = self.lid_output.lock().map_err(poisoned)?.action(
            inputs,
            now_ms,
            store.map_or(1000, |store| store.lid_animation_duration_ms("lid_open")),
            store.map_or(1300, |store| store.lid_animation_duration_ms("lid_closed")),
        );
        if display == "custom" {
            return Ok(Some(false));
        }
        match action {
            sidepulse_core::LidOutputAction::Hold => return Ok(Some(false)),
            sidepulse_core::LidOutputAction::Transition { state } => {
                let (style, custom) = if let Some(store) = store {
                    store.animation_for_state(state)?
                } else {
                    (
                        sidepulse_core::default_animation(state).into(),
                        String::new(),
                    )
                };
                let program = program_for_style(
                    mode,
                    led_count_for_target(output.target()),
                    output.brightness(),
                    &style,
                    &custom,
                )?;
                return output.sync_program(&program).map(Some);
            }
            sidepulse_core::LidOutputAction::Live => {}
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
        // Darwin can inherit O_NONBLOCK from the listening socket. Only accept
        // is polled; each connection worker needs blocking framed I/O.
        stream.set_nonblocking(false)?;
        #[cfg(unix)]
        {
            stream.set_recv_timeout(Some(Duration::from_secs(5)))?;
            stream.set_send_timeout(Some(Duration::from_secs(5)))?;
        }
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
            RequestKind::Shutdown => {
                write_message(
                    &mut stream,
                    &ServerMessage {
                        version: PROTOCOL_VERSION,
                        request_id: Some(request.request_id),
                        payload: ServerPayload::Ack,
                    },
                )?;
                self.shutdown
                    .store(true, std::sync::atomic::Ordering::Release);
                Ok(())
            }
            RequestKind::RenderLedProgram {
                source,
                led_count,
                full_watts,
            } => {
                let result = (|| -> io::Result<String> {
                    if source == "agent" {
                        return Ok(sidepulse_device::program_for_mode(
                            self.snapshot()?.aggregate.mode,
                            usize::from(led_count),
                            255,
                        ));
                    }
                    let saved_baseline = self
                        .settings
                        .lock()
                        .map_err(poisoned)?
                        .as_ref()
                        .and_then(SettingsStore::battery_full_charge_watts);
                    let baseline = match &full_watts {
                        Some(sidepulse_core::ChargerBaseline::Auto) => None,
                        Some(sidepulse_core::ChargerBaseline::Watts { watts }) => Some(*watts),
                        None => saved_baseline,
                    };
                    let battery = if full_watts.is_some() {
                        read_battery_snapshot(baseline)?
                    } else {
                        self.battery_diagnostics
                            .lock()
                            .map_err(poisoned)?
                            .clone()
                            .map_or_else(|| read_battery_snapshot(baseline), Ok)?
                    };
                    let mut state = battery.led_state().ok_or_else(|| {
                        io::Error::new(io::ErrorKind::NotFound, "no battery is present")
                    })?;
                    if let Some(watts) = baseline {
                        state.full_charge_watts = watts;
                    }
                    Ok(program_for_battery(state, usize::from(led_count), 360, 255))
                })();
                let payload = result.map_or_else(
                    |error| ServerPayload::Error {
                        code: "led_program_failed".into(),
                        message: error.to_string(),
                    },
                    |program| ServerPayload::LedProgram { program },
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
            RequestKind::History => write_message(
                &mut stream,
                &ServerMessage {
                    version: PROTOCOL_VERSION,
                    request_id: Some(request.request_id),
                    payload: self.history_snapshot()?,
                },
            ),
            RequestKind::PhoneLinks
            | RequestKind::SetPhoneDisplay { .. }
            | RequestKind::RegisterPhone { .. }
            | RequestKind::RemovePhone { .. }
            | RequestKind::BeginPhonePairing { .. }
            | RequestKind::CancelPhonePairing
            | RequestKind::ReloadPhoneLinks => {
                let result = match request.kind {
                    RequestKind::PhoneLinks => Ok(()),
                    RequestKind::SetPhoneDisplay { id, display } => {
                        self.set_phone_display(&id, &display)
                    }
                    RequestKind::RegisterPhone {
                        token,
                        name,
                        server,
                    } => self.register_phone(&token, &name, server.as_deref()),
                    RequestKind::RemovePhone { id } => self.remove_phone(&id),
                    RequestKind::BeginPhonePairing { server } => {
                        self.begin_phone_pairing(server.as_deref())
                    }
                    RequestKind::CancelPhonePairing => self.cancel_phone_pairing(),
                    RequestKind::ReloadPhoneLinks => self.reload_phone_links(),
                    _ => unreachable!(),
                };
                let payload = result
                    .and_then(|_| self.phone_links_snapshot())
                    .unwrap_or_else(|error| ServerPayload::Error {
                        code: "phone_links_failed".into(),
                        message: error.to_string(),
                    });
                write_message(
                    &mut stream,
                    &ServerMessage {
                        version: PROTOCOL_VERSION,
                        request_id: Some(request.request_id),
                        payload,
                    },
                )
            }
            RequestKind::Deliver { request: delivery } => {
                let payload =
                    self.start_delivery(&delivery)
                        .unwrap_or_else(|error| ServerPayload::Error {
                            code: "delivery_failed".into(),
                            message: error.to_string(),
                        });
                write_message(
                    &mut stream,
                    &ServerMessage {
                        version: PROTOCOL_VERSION,
                        request_id: Some(request.request_id),
                        payload,
                    },
                )
            }
            RequestKind::DeliveryStatus { id } => {
                let payload =
                    self.delivery_status(&id)
                        .unwrap_or_else(|error| ServerPayload::Error {
                            code: "delivery_failed".into(),
                            message: error.to_string(),
                        });
                write_message(
                    &mut stream,
                    &ServerMessage {
                        version: PROTOCOL_VERSION,
                        request_id: Some(request.request_id),
                        payload,
                    },
                )
            }
            RequestKind::RelaySettings
            | RequestKind::SetRelaySettings { .. }
            | RequestKind::ReloadRelaySettings => {
                let result = match request.kind {
                    RequestKind::RelaySettings => Ok(()),
                    RequestKind::SetRelaySettings { patch } => self.set_relay_settings(&patch),
                    RequestKind::ReloadRelaySettings => self.reload_relay_settings(),
                    _ => unreachable!(),
                };
                let payload = result.and_then(|_| self.relay_settings()).map_or_else(
                    |error| ServerPayload::Error {
                        code: if error.kind() == io::ErrorKind::AlreadyExists {
                            "relay_settings_conflict"
                        } else {
                            "relay_settings_failed"
                        }
                        .into(),
                        message: error.to_string(),
                    },
                    |settings| ServerPayload::RelaySettings { settings },
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
            RequestKind::AnimationLibrary | RequestKind::ExportAnimationProfile { .. } => {
                let result = match request.kind {
                    RequestKind::AnimationLibrary => self
                        .animation_library()
                        .map(|library| ServerPayload::AnimationLibrary { library }),
                    RequestKind::ExportAnimationProfile { id } => self
                        .export_animation_profile(id.as_deref())
                        .map(|document| ServerPayload::AnimationProfileDocument { document }),
                    _ => unreachable!(),
                };
                let payload = result.unwrap_or_else(|error| ServerPayload::Error {
                    code: "animation_library_unavailable".into(),
                    message: error.to_string(),
                });
                write_message(
                    &mut stream,
                    &ServerMessage {
                        version: PROTOCOL_VERSION,
                        request_id: Some(request.request_id),
                        payload,
                    },
                )
            }
            RequestKind::SessionTargets { agent_id, action } => {
                let payload = self
                    .session_targets(&agent_id, action)
                    .unwrap_or_else(|error| ServerPayload::Error {
                        code: "session_unavailable".into(),
                        message: error.to_string(),
                    });
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
            RequestKind::PowerControl | RequestKind::RetryPowerControl => {
                let mut status = self.power_control_status.lock().map_err(poisoned)?.clone();
                status.supported = cfg!(target_os = "macos");
                let payload =
                    if matches!(request.kind, RequestKind::RetryPowerControl) && !status.enabled {
                        ServerPayload::Error {
                            code: "power_control_disabled".into(),
                            message: "Power control is paused in this session.".into(),
                        }
                    } else {
                        if matches!(request.kind, RequestKind::RetryPowerControl) {
                            self.power_retry_generation
                                .fetch_add(1, std::sync::atomic::Ordering::AcqRel);
                        }
                        ServerPayload::PowerControl { status }
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
                let observation = self
                    .power_observation
                    .lock()
                    .map_err(poisoned)?
                    .clone()
                    .map_or_else(power::observe, Ok);
                let payload = match observation {
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
            kind @ (RequestKind::SetLidAnimationTiming { .. }
            | RequestKind::EditAnimationLibrary { .. }
            | RequestKind::SetAnimationState { .. }
            | RequestKind::SetAgentAnimation { .. }
            | RequestKind::SetSessionOpenPreference { .. }
            | RequestKind::SetSessionTerminal { .. }
            | RequestKind::SetHistoryTimeframe { .. }
            | RequestKind::SetVirtualDisplay { .. }
            | RequestKind::SetBatterySettings { .. }
            | RequestKind::SetAgentListSettings { .. }
            | RequestKind::SetSleepSettings { .. }) => {
                let result = match kind {
                    RequestKind::SetLidAnimationTiming {
                        open_seconds,
                        close_seconds,
                    } => self.set_lid_animation_timing(open_seconds, close_seconds),
                    RequestKind::EditAnimationLibrary { edit } => {
                        self.edit_animation_library(&edit)
                    }
                    RequestKind::SetAnimationState {
                        state,
                        style,
                        custom_program,
                    } => self.set_animation_state(&state, &style, custom_program.as_deref()),
                    RequestKind::SetSessionOpenPreference {
                        provider,
                        origin,
                        action,
                    } => self.set_session_open_preference(&provider, origin.as_deref(), action),
                    RequestKind::SetSessionTerminal {
                        terminal,
                        custom_path,
                    } => self.set_session_terminal(&terminal, custom_path.as_deref()),
                    RequestKind::SetHistoryTimeframe { seconds } => {
                        self.set_history_timeframe(seconds)
                    }
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
                while self.running() {
                    match receiver.recv_timeout(Duration::from_millis(100)) {
                        Ok(()) => {}
                        Err(mpsc::RecvTimeoutError::Timeout) => continue,
                        Err(mpsc::RecvTimeoutError::Disconnected) => break,
                    }
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
            phone_links_path: None,
            phone_output: false,
            power_control: false,
            power_observation_path: None,
            history_path: None,
        },
    )
}

fn read_mock_power(path: &Path) -> io::Result<sidepulse_core::PowerSnapshot> {
    use std::io::Read;
    let mut bytes = Vec::new();
    File::open(path)?.take(65537).read_to_end(&mut bytes)?;
    if bytes.len() > 65536 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "power observation is too large",
        ));
    }
    serde_json::from_slice(&bytes).map_err(io::Error::other)
}

pub struct RunOptions<'a> {
    pub logs: &'a [(String, PathBuf)],
    pub device: Option<(&'a Path, Option<u8>)>,
    pub latest_state_path: Option<&'a Path>,
    pub settings_path: Option<&'a Path>,
    pub auto_device: bool,
    pub relay_config_path: Option<&'a Path>,
    pub phone_links_path: Option<&'a Path>,
    pub phone_output: bool,
    pub power_control: bool,
    pub power_observation_path: Option<&'a Path>,
    pub history_path: Option<&'a Path>,
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
    run_with_shutdown(endpoint, options, Arc::default())
}

/// The executable's signal handler and the IPC shutdown request share this flag.
pub fn run_with_shutdown(
    endpoint: &str,
    options: RunOptions<'_>,
    shutdown: Arc<std::sync::atomic::AtomicBool>,
) -> io::Result<()> {
    let mut workers = RuntimeWorkers {
        shutdown: shutdown.clone(),
        threads: Vec::new(),
    };
    let RunOptions {
        logs,
        device,
        latest_state_path,
        settings_path,
        auto_device,
        relay_config_path,
        phone_links_path,
        phone_output,
        power_control,
        power_observation_path,
        history_path,
    } = options;
    if phone_output && (phone_links_path.is_none() || settings_path.is_none()) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "--phone-output requires --phone-links and --settings",
        ));
    }
    if power_control && power_observation_path.is_some() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "power simulation cannot be combined with system power control",
        ));
    }
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
    let mut service = latest_state_path.map_or_else(Service::new, |path| {
        Service::with_state_path(path.to_path_buf())
    });
    service.shutdown = shutdown;
    if let Some(path) = settings_path {
        service.configure_settings(path)?;
    }
    if let Some(path) = power_observation_path {
        service.observe_power(&read_mock_power(path)?)?;
    }
    let history_path = history_path.map(Path::to_path_buf).or_else(|| {
        latest_state_path
            .or(settings_path)
            .and_then(Path::parent)
            .map(|parent| parent.join("status-history.jsonl"))
    });
    if let Some(path) = &history_path {
        service.configure_history(path)?;
    }
    #[cfg(target_os = "macos")]
    if power_control {
        use sidepulse_core::{BatteryPower, SleepInputs, battery_safeguard_active, plan_sleep};

        service
            .power_control_status
            .lock()
            .map_err(poisoned)?
            .enabled = true;
        let power_service = service.clone();
        workers.threads.push(std::thread::spawn(move || {
            let mut controller = power::MacPowerController::new();
            let mut retry_generation = power_service
                .power_retry_generation
                .load(std::sync::atomic::Ordering::Acquire);
            let mut activity = sidepulse_core::AwakeActivity::default();
            let started = Instant::now();
            let mut last_error = None;
            while power_service.running() {
                let generation = power_service
                    .power_retry_generation
                    .load(std::sync::atomic::Ordering::Acquire);
                if generation != retry_generation {
                    controller.retry_helper();
                    retry_generation = generation;
                }
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
                    let plan =
                        plan_sleep(SleepInputs {
                            policy,
                            agents_active: Some(activity.requested(
                                state.aggregate.mode,
                                started.elapsed().as_millis() as u64,
                            )),
                            battery_safeguard_active: battery_safeguard_active(battery, threshold),
                            lid_closed: observation.lid_closed,
                            external_display_active: observation.external_display_active,
                        });
                    let result = controller.sync(plan, allow_override);
                    *power_service.history_power.lock().map_err(poisoned)? = HistoryPowerState {
                        requested: plan.hold_caffeinate,
                        active: controller.active(),
                        closed_lid_requested: allow_override && plan.disable_system_sleep,
                        closed_lid_active: controller.system_sleep_disabled(),
                    };
                    if let Ok(mut health) = power_service.power_control_status.lock() {
                        health.requested = plan.hold_caffeinate;
                        health.active = controller.active();
                        health.closed_lid_requested = allow_override && plan.disable_system_sleep;
                        health.closed_lid_active = controller.system_sleep_disabled();
                    }
                    result
                })();
                if let Ok(mut health) = power_service.power_control_status.lock() {
                    health.checked_at = Some(Utc::now());
                    health.error = result.as_ref().err().map(ToString::to_string);
                }
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
                power_service.sleep_while_running(Duration::from_secs(1));
            }
        }));
    }
    if let Some(path) = phone_links_path {
        service.configure_phone_links(path)?;
        if phone_output {
            service.enable_phone_output()?;
            let phone_service = service.clone();
            workers.threads.push(std::thread::spawn(move || {
                while phone_service.running() {
                    if let Err(error) = phone_service.sync_phone_outputs() {
                        eprintln!("sidepulse-next-service: phone output: {error}");
                    }
                    phone_service.sleep_while_running(Duration::from_secs(1));
                }
            }));
        }
    }
    if let Some(path) = relay_config_path {
        use std::sync::atomic::Ordering;
        service.configure_relay(path)?;
        let (sender, receiver) = mpsc::sync_channel(128);
        *service.relay_publisher.lock().map_err(poisoned)? = Some(sender);
        let publish_service = service.clone();
        workers.threads.push(std::thread::spawn(move || {
            while publish_service.running() {
                let (provider, line) = match receiver.recv_timeout(Duration::from_millis(100)) {
                    Ok(event) => event,
                    Err(mpsc::RecvTimeoutError::Timeout) => continue,
                    Err(mpsc::RecvTimeoutError::Disconnected) => break,
                };
                let result = publish_service.relay_config().and_then(|config| {
                    if let Some(config) = config
                        && !config.outbound_channel.is_empty()
                    {
                        publish_event(&config, &provider, &line)?;
                        let mut health = publish_service.relay_health.lock().map_err(poisoned)?;
                        health.last_sent_at = Some(Utc::now());
                        health.send_error = None;
                    }
                    Ok(())
                });
                if let Err(error) = result {
                    if let Ok(mut health) = publish_service.relay_health.lock() {
                        health.send_error = Some(error.to_string());
                    }
                    eprintln!("sidepulse-next-service: relay send failed: {error}");
                }
            }
        }));
        let receiver_service = service.clone();
        workers.threads.push(std::thread::spawn(move || {
            let mut last_error = None;
            while receiver_service.running() {
                let generation = receiver_service.relay_generation.load(Ordering::Acquire);
                let result = receiver_service.relay_config().and_then(|config| {
                    if let Some(config) = config
                        && !config.receiver_channel.is_empty()
                    {
                        receive_once_while(
                            &config,
                            || {
                                receiver_service.running()
                                    && generation
                                        == receiver_service.relay_generation.load(Ordering::Acquire)
                            },
                            |message| {
                                let _configuration = receiver_service
                                    .relay_config_store
                                    .lock()
                                    .map_err(poisoned)?;
                                if !receiver_service.running()
                                    || generation
                                        != receiver_service.relay_generation.load(Ordering::Acquire)
                                {
                                    return Ok(());
                                }
                                if receiver_service.ingest_relay_message(&message)? {
                                    let mut health =
                                        receiver_service.relay_health.lock().map_err(poisoned)?;
                                    health.last_received_at = Some(Utc::now());
                                    health.receive_error = None;
                                }
                                Ok(())
                            },
                        )?;
                    }
                    Ok(())
                });
                if generation != receiver_service.relay_generation.load(Ordering::Acquire) {
                    last_error = None;
                    continue;
                }
                if let Err(error) = result {
                    let message = error.to_string();
                    if let Ok(mut health) = receiver_service.relay_health.lock() {
                        health.receive_error = Some(message.clone());
                    }
                    if last_error.as_deref() != Some(message.as_str()) {
                        eprintln!("sidepulse-next-service: relay receive failed: {message}");
                    }
                    last_error = Some(message);
                } else {
                    last_error = None;
                }
                receiver_service.sleep_while_running(Duration::from_secs(1));
            }
        }));
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
    workers.threads.push(std::thread::spawn(move || {
        let mut last_error = None;
        while recovery_service.running() {
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
            recovery_service.sleep_while_running(Duration::from_secs(1));
        }
    }));
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
        workers.threads.push(std::thread::spawn(move || {
            while selection_service.running() {
                if let Err(error) = selection_service.auto_select_device(&roots) {
                    eprintln!("sidepulse-next-service: device discovery: {error}");
                }
                selection_service.sleep_while_running(Duration::from_secs(5));
            }
        }));
    }
    if device.is_some() || auto_device {
        let output_service = service.clone();
        workers.threads.push(std::thread::spawn(move || {
            let mut last_error = None;
            while output_service.running() {
                if let Some(output) = output_service.device.lock().ok().and_then(|mut device| {
                    device
                        .as_mut()
                        .map(|output| output.poke_keepalive(Instant::now()))
                }) && let Err(error) = output
                {
                    eprintln!("sidepulse-next-service: device keepalive: {error}");
                }
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
                output_service.sleep_while_running(Duration::from_secs(1));
            }
        }));
    }
    if device.is_some()
        || auto_device
        || power_control
        || settings_path.is_some()
        || history_path.is_some()
    {
        let battery_service = service.clone();
        workers.threads.push(std::thread::spawn(move || {
            let mut last_error = None;
            while battery_service.running() {
                let result = read_battery_snapshot(None)
                    .and_then(|battery| battery_service.update_battery_snapshot(battery));
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
                battery_service.sleep_while_running(Duration::from_secs(1));
            }
        }));
    }
    if power_observation_path.is_some()
        || history_path.is_some()
        || device.is_some()
        || auto_device
        || settings_path.is_some()
        || power_control
    {
        let record_history = history_path.is_some();
        let power_observation_path = power_observation_path.map(Path::to_path_buf);
        let history_service = service.clone();
        workers.threads.push(std::thread::spawn(move || {
            let mut last_error = None;
            while history_service.running() {
                let result = (|| -> io::Result<()> {
                    let observation = if let Some(path) = &power_observation_path {
                        Some(read_mock_power(path)?)
                    } else {
                        power::observe().ok()
                    };
                    if let Some(observation) = &observation {
                        history_service.observe_power(observation)?;
                    }
                    if record_history {
                        history_service.record_history(observation.as_ref())?;
                    }
                    Ok(())
                })();
                match result {
                    Ok(()) => last_error = None,
                    Err(error) => {
                        let message = error.to_string();
                        if last_error.as_deref() != Some(message.as_str()) {
                            eprintln!("sidepulse-next-service: history: {message}");
                        }
                        last_error = Some(message);
                    }
                }
                history_service.sleep_while_running(Duration::from_secs(2));
            }
        }));
    }
    listener.set_nonblocking(interprocess::local_socket::ListenerNonblockingMode::Accept)?;
    while service.running() {
        let stream = match listener.accept() {
            Ok(stream) => stream,
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                service.sleep_while_running(Duration::from_millis(50));
                continue;
            }
            Err(error) => return Err(error),
        };
        let service = service.clone();
        std::thread::spawn(move || {
            if let Err(error) = service.serve_connection(stream) {
                eprintln!("sidepulse-next-service: connection failed: {error}");
            }
        });
    }
    drop(workers);
    service.persist_latest_state()?;
    Ok(())
}

struct RuntimeWorkers {
    shutdown: Arc<std::sync::atomic::AtomicBool>,
    threads: Vec<std::thread::JoinHandle<()>>,
}
impl Drop for RuntimeWorkers {
    fn drop(&mut self) {
        self.shutdown
            .store(true, std::sync::atomic::Ordering::Release);
        for worker in self.threads.drain(..) {
            if worker.join().is_err() {
                eprintln!("sidepulse-next-service: a worker failed during shutdown");
            }
        }
    }
}
impl Service {
    fn running(&self) -> bool {
        !self.shutdown.load(std::sync::atomic::Ordering::Acquire)
    }
    fn sleep_while_running(&self, duration: Duration) {
        let deadline = Instant::now() + duration;
        while self.running() && Instant::now() < deadline {
            std::thread::sleep(
                Duration::from_millis(50).min(deadline.saturating_duration_since(Instant::now())),
            );
        }
    }
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
    fn accepted_nonblocking_stream_recovers_delayed_requests_and_large_history_replies() {
        use interprocess::local_socket::ListenerNonblockingMode;
        use std::sync::Barrier;
        let directory = tempfile::tempdir().unwrap();
        let endpoint = if cfg!(windows) {
            format!("sidepulse-history-{}", uuid::Uuid::new_v4())
        } else {
            // Keep the Unix socket pathname below the platform limit.
            format!("/tmp/sidepulse-history-{}.sock", uuid::Uuid::new_v4())
        };
        let history = directory.path().join("history.jsonl");
        let start = Utc::now();
        let rows = (0..2000)
            .map(|index| {
                serde_json::json!({
                    "recorded_at": (start + chrono::Duration::seconds(index)).to_rfc3339(),
                    "agent_status": "working", "display_status": "Working",
                    "battery_level": 50.0, "charger_power_watts": 90.0,
                    "lid_closed": false, "sidepulse_keep_awake_active": false,
                })
                .to_string()
            })
            .collect::<Vec<_>>()
            .join("\n");
        fs::write(&history, rows).unwrap();
        let service = Service::new();
        service.configure_history(&history).unwrap();
        let listener = sidepulse_ipc::bind(&endpoint).unwrap();
        listener
            .set_nonblocking(ListenerNonblockingMode::Accept)
            .unwrap();
        let accepted = Arc::new(Barrier::new(2));
        let ready = accepted.clone();
        let server = std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(3);
            let stream = loop {
                match listener.accept() {
                    Ok(stream) => break stream,
                    Err(error)
                        if error.kind() == io::ErrorKind::WouldBlock
                            && Instant::now() < deadline =>
                    {
                        std::thread::sleep(Duration::from_millis(5))
                    }
                    Err(error) => panic!("accept: {error}"),
                }
            };
            // Reproduce Darwin's inherited listener flag on every platform.
            stream.set_nonblocking(true).unwrap();
            ready.wait();
            service.serve_connection(stream).unwrap();
        });
        let mut client = sidepulse_ipc::connect(&endpoint, Duration::from_secs(3)).unwrap();
        accepted.wait();
        std::thread::sleep(Duration::from_millis(50));
        write_message(
            &mut client,
            &ClientRequest {
                version: PROTOCOL_VERSION,
                request_id: 8,
                kind: RequestKind::History,
            },
        )
        .unwrap();
        std::thread::sleep(Duration::from_millis(50));
        let response: ServerMessage = read_message(&mut BufReader::new(client)).unwrap();
        assert_eq!(response.request_id, Some(8));
        let ServerPayload::History {
            points, sampled, ..
        } = response.payload
        else {
            panic!("missing history");
        };
        assert!(!sampled);
        assert_eq!(points.len(), 2000);
        server.join().unwrap();
    }

    #[test]
    fn history_record_matches_python_schema_and_survives_restart() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("history.jsonl");
        let service = Service::new();
        service.configure_history(&path).unwrap();
        service.record_history(None).unwrap();
        let mut recorded: serde_json::Value =
            serde_json::from_str(fs::read_to_string(&path).unwrap().trim()).unwrap();
        recorded["recorded_at"] = "2026-01-01T00:00:00Z".into();
        let expected: serde_json::Value = serde_json::from_str(include_str!(
            "../resources/fixtures/history-idle.expected.json"
        ))
        .unwrap();
        assert_eq!(recorded, expected);
        let recovered = Service::new();
        recovered.configure_history(&path).unwrap();
        let ServerPayload::History { points, .. } = recovered.history_snapshot().unwrap() else {
            panic!("missing history");
        };
        assert_eq!(points.len(), 1);
        assert_eq!(points[0].agent_status, sidepulse_core::AgentMode::IdleReady);
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
    #[test]
    fn lid_output_is_owned_by_service_and_respects_manual_display() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("settings.json");
        fs::write(&path, r#"{"sleep_prevention_policy":"never"}"#).unwrap();
        let target = directory.path().join("LEDS.LED");
        let service = Service::new();
        service.configure_settings(&path).unwrap();
        service.configure_device(&target, 255).unwrap();
        service
            .set_animation_state("idle_ready", "custom", Some("#111111"))
            .unwrap();
        service
            .set_animation_state("lid_closed", "custom", Some("#222222"))
            .unwrap();
        service
            .set_animation_state("lid_open", "custom", Some("#333333"))
            .unwrap();
        let observation = |closed| sidepulse_core::PowerSnapshot {
            lid_closed: Some(closed),
            ..Default::default()
        };
        let now = Instant::now();
        service.observe_power(&observation(false)).unwrap();
        assert_eq!(
            service.render_device_with_battery_at(None, now).unwrap(),
            Some(true)
        );
        assert_eq!(fs::read_to_string(&target).unwrap(), "#111111");
        service.observe_power(&observation(true)).unwrap();
        assert_eq!(
            service
                .render_device_with_battery_at(None, now + Duration::from_millis(10))
                .unwrap(),
            Some(true)
        );
        assert_eq!(fs::read_to_string(&target).unwrap(), "#222222");
        assert_eq!(
            service
                .render_device_with_battery_at(None, now + Duration::from_secs(2))
                .unwrap(),
            Some(false)
        );
        fs::write(&target, "external frame").unwrap();
        assert_eq!(
            service
                .render_device_with_battery_at(None, now + Duration::from_secs(3))
                .unwrap(),
            Some(false)
        );
        assert_eq!(fs::read_to_string(&target).unwrap(), "external frame");
        service.observe_power(&observation(false)).unwrap();
        assert_eq!(
            service
                .render_device_with_battery_at(None, now + Duration::from_secs(4))
                .unwrap(),
            Some(true)
        );
        assert_eq!(fs::read_to_string(&target).unwrap(), "#333333");
        assert_eq!(
            service
                .render_device_with_battery_at(None, now + Duration::from_secs(6))
                .unwrap(),
            Some(true)
        );
        assert_eq!(fs::read_to_string(&target).unwrap(), "#111111");
        service.set_display_mode("custom").unwrap();
        fs::write(&target, "manual output").unwrap();
        service.observe_power(&observation(true)).unwrap();
        assert_eq!(
            service
                .render_device_with_battery_at(None, now + Duration::from_secs(7))
                .unwrap(),
            Some(false)
        );
        assert_eq!(fs::read_to_string(&target).unwrap(), "manual output");
    }

    #[test]
    fn closed_lid_awake_policy_skips_transition_without_starting_power_control() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("settings.json");
        fs::write(&path, r#"{"sleep_prevention_policy":"always"}"#).unwrap();
        let target = directory.path().join("LEDS.LED");
        let service = Service::new();
        service.configure_settings(&path).unwrap();
        service.configure_device(&target, 255).unwrap();
        let now = Instant::now();
        service
            .observe_power(&sidepulse_core::PowerSnapshot {
                lid_closed: Some(false),
                ..Default::default()
            })
            .unwrap();
        service.render_device_with_battery_at(None, now).unwrap();
        let before = fs::read(&target).unwrap();
        service
            .observe_power(&sidepulse_core::PowerSnapshot {
                lid_closed: Some(true),
                ..Default::default()
            })
            .unwrap();
        assert_eq!(
            service
                .render_device_with_battery_at(None, now + Duration::from_secs(1))
                .unwrap(),
            Some(false)
        );
        assert_eq!(fs::read(target).unwrap(), before);
    }
    #[test]
    fn lid_timing_is_atomic_and_preserves_programs_and_unknown_fields() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("settings.json");
        fs::write(&path, r##"{"other":7,"lid_open_animation":{"program":"#123456","other":"keep"},"lid_closed_animation":{"program":"off"}}"##).unwrap();
        let service = Service::new();
        service.configure_settings(&path).unwrap();
        service
            .set_lid_animation_timing(Some(2.0), Some(3.0))
            .unwrap();
        let bytes = fs::read(&path).unwrap();
        let saved: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(saved["lid_open_animation"]["duration_seconds"], 2.0);
        assert_eq!(saved["lid_closed_animation"]["duration_seconds"], 3.0);
        assert_eq!(saved["lid_open_animation"]["program"], "#123456");
        assert_eq!(saved["lid_open_animation"]["other"], "keep");
        assert_eq!(saved["other"], 7);
        assert!(
            service
                .set_lid_animation_timing(Some(4.0), Some(f64::NAN))
                .is_err()
        );
        assert_eq!(fs::read(&path).unwrap(), bytes);
    }
}
