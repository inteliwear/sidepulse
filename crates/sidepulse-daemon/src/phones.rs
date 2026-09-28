//! Phone pairing and delivery jobs owned by the headless service.
use super::{Service, poisoned};
use chrono::Utc;
use sidepulse_core::{
    DeliveryDestination, DeliveryJobView, DeliveryOutcome, DeliveryRequest, DestinationKind,
    PhonePairingView, ServerPayload,
};
use sidepulse_links::{PhoneLink, PhoneStore};
use sidepulse_relay::DEFAULT_BRIDGE_SERVER;
use std::{
    collections::BTreeMap,
    io,
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

#[derive(Default)]
pub(super) struct PhoneOutputState {
    enabled: bool,
    targets: BTreeMap<String, PhoneOutput>,
}
#[derive(Default)]
struct PhoneOutput {
    last_program: Option<String>,
    last_sent_at: Option<chrono::DateTime<Utc>>,
    error: Option<String>,
    retry_after: Option<Instant>,
}
pub(super) struct PairingRuntime {
    view: PhonePairingView,
    cancel: Arc<AtomicBool>,
}
fn unavailable() -> io::Error {
    io::Error::new(io::ErrorKind::NotFound, "no phone links path is configured")
}
impl Service {
    pub fn configure_phone_links(&self, path: &Path) -> io::Result<()> {
        *self.phone_links.lock().map_err(poisoned)? = Some(PhoneStore::load(path)?);
        Ok(())
    }
    pub fn reload_phone_links(&self) -> io::Result<()> {
        let path = self
            .phone_links
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .ok_or_else(unavailable)?
            .path()
            .to_owned();
        self.configure_phone_links(&path)
    }
    pub fn phone_links_snapshot(&self) -> io::Result<ServerPayload> {
        let store = self.phone_links.lock().map_err(poisoned)?;
        let configured = store.is_some();
        let mut links: Vec<_> = store.as_ref().map_or_else(Vec::new, |store| {
            store.links().iter().map(PhoneLink::summary).collect()
        });
        let credentials = store.as_ref().map_or_else(Vec::new, PhoneStore::links);
        drop(store);
        let settings = self.settings.lock().map_err(poisoned)?;
        let output = self.phone_output.lock().map_err(poisoned)?;
        for (link, credential) in links.iter_mut().zip(credentials) {
            link.display = settings
                .as_ref()
                .map_or("agent", |settings| settings.display_for_phone(&link.id))
                .into();
            if let Some(target) = output.targets.get(&credential.token) {
                link.last_sent_at = target.last_sent_at;
                link.delivery_error = target.error.clone();
            }
        }
        let output_enabled = output.enabled;
        drop(output);
        drop(settings);
        let pairing = self
            .phone_pairing
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .map(|pairing| pairing.view.clone());
        Ok(ServerPayload::PhoneLinks {
            configured,
            output_enabled,
            links,
            pairing,
        })
    }
    pub fn set_phone_display(&self, id: &str, display: &str) -> io::Result<()> {
        let links = self
            .phone_links
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .ok_or_else(unavailable)?
            .links();
        let matching: Vec<_> = links
            .iter()
            .filter(|link| link.summary().id == id)
            .collect();
        if matching.len() != 1 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "phone identifier is missing or ambiguous",
            ));
        }
        self.settings
            .lock()
            .map_err(poisoned)?
            .as_mut()
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
            })?
            .set_phone_display(id, &matching[0].name, display)?;
        if let Some(output) = self
            .phone_output
            .lock()
            .map_err(poisoned)?
            .targets
            .get_mut(&matching[0].token)
        {
            output.last_program = None;
            output.retry_after = None;
        }
        Ok(())
    }
    pub fn enable_phone_output(&self) -> io::Result<()> {
        if self.settings.lock().map_err(poisoned)?.is_none()
            || self.phone_links.lock().map_err(poisoned)?.is_none()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "phone output requires settings and saved links",
            ));
        }
        self.phone_output.lock().map_err(poisoned)?.enabled = true;
        Ok(())
    }
    fn phone_is_current(&self, link: &PhoneLink) -> io::Result<bool> {
        if !self.running() {
            return Ok(false);
        }
        Ok(self
            .phone_links
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .is_some_and(|store| {
                store
                    .links()
                    .iter()
                    .any(|current| current.token == link.token && current.server == link.server)
            }))
    }
    fn phone_program(&self, link: &PhoneLink) -> io::Result<Option<String>> {
        let mode = self.snapshot()?.aggregate.mode;
        let battery = self.battery_preview.lock().map_err(poisoned)?.latest;
        let settings = self.settings.lock().map_err(poisoned)?;
        let store = settings.as_ref().ok_or_else(|| {
            io::Error::new(io::ErrorKind::NotFound, "no settings path is configured")
        })?;
        let display = store.display_for_phone(&link.summary().id);
        let display = self
            .battery_preview
            .lock()
            .map_err(poisoned)?
            .display(display, Instant::now())
            .to_owned();
        if display == "custom" {
            return Ok(None);
        }
        if display == "battery" {
            return Ok(battery.map(|mut state| {
                if let Some(watts) = store.battery_full_charge_watts() {
                    state.full_charge_watts = watts;
                }
                sidepulse_device::battery::program_for_battery(state, 8, 360, 255)
            }));
        }
        let (style, custom) = store.animation_for_mode(mode)?;
        sidepulse_device::animations::program_for_style(mode, 8, 255, &style, &custom).map(Some)
    }
    fn phone_data(&self) -> serde_json::Value {
        let source = self.relay_config().ok().flatten().map_or_else(
            || "Remote computer".to_owned(),
            |config| config.machine_name,
        );
        let mut data = serde_json::json!({"source":{"name":source}});
        if let Ok(battery) = self.battery_diagnostics.lock()
            && let Some(battery) = battery.as_ref().filter(|battery| battery.battery_present)
        {
            data["source"]["battery"] = serde_json::json!({"level":battery.percent,"charging":battery.is_charging,"plugged_in":battery.is_plugged});
        }
        data
    }
    fn send_phone_locked(
        &self,
        link: &PhoneLink,
        request: &DeliveryRequest,
        event_id: &str,
        data: &serde_json::Value,
    ) -> io::Result<()> {
        let result = sidepulse_links::send_program(
            link,
            request.program.as_deref(),
            request.title.as_deref(),
            request.message.as_deref(),
            event_id,
            data,
        )
        .map(|_| ());
        let mut state = self.phone_output.lock().map_err(poisoned)?;
        let target = state.targets.entry(link.token.clone()).or_default();
        match &result {
            Ok(()) => {
                target.last_program = request.program.clone();
                target.last_sent_at = Some(Utc::now());
                target.error = None;
                target.retry_after = None;
            }
            Err(error) => {
                target.error = Some(error.to_string());
                target.retry_after = Some(Instant::now() + Duration::from_secs(30));
            }
        }
        result
    }
    fn deliver_phone(
        &self,
        link: &PhoneLink,
        request: &DeliveryRequest,
        event_id: &str,
        data: &serde_json::Value,
    ) -> io::Result<()> {
        let _gate = self.phone_send_gate.lock().map_err(poisoned)?;
        if !self.phone_is_current(link)? {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "phone link changed before delivery",
            ));
        }
        if request.program.is_some()
            && let Some(store) = self.settings.lock().map_err(poisoned)?.as_mut()
            && store.display_for_phone(&link.summary().id) != "custom"
        {
            store.set_phone_display(&link.summary().id, &link.name, "custom")?;
        }
        self.send_phone_locked(link, request, event_id, data)
    }
    pub fn sync_phone_outputs(&self) -> io::Result<()> {
        if !self.phone_output.lock().map_err(poisoned)?.enabled {
            return Ok(());
        }
        let links = self
            .phone_links
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .ok_or_else(unavailable)?
            .links();
        self.phone_output
            .lock()
            .map_err(poisoned)?
            .targets
            .retain(|token, _| links.iter().any(|link| link.token == *token));
        for link in links {
            let _gate = self.phone_send_gate.lock().map_err(poisoned)?;
            if !self.phone_is_current(&link)? {
                continue;
            }
            let program = match self.phone_program(&link) {
                Ok(Some(program)) => program,
                Ok(None) => {
                    if let Some(target) = self
                        .phone_output
                        .lock()
                        .map_err(poisoned)?
                        .targets
                        .get_mut(&link.token)
                    {
                        target.last_program = None;
                    }
                    continue;
                }
                Err(error) => {
                    self.phone_output
                        .lock()
                        .map_err(poisoned)?
                        .targets
                        .entry(link.token.clone())
                        .or_default()
                        .error = Some(error.to_string());
                    continue;
                }
            };
            let should_send = self
                .phone_output
                .lock()
                .map_err(poisoned)?
                .targets
                .get(&link.token)
                .is_none_or(|target| {
                    target.last_program.as_deref() != Some(&program)
                        && target.retry_after.is_none_or(|at| Instant::now() >= at)
                });
            if !should_send {
                continue;
            }
            let request = DeliveryRequest {
                program: Some(program),
                ..Default::default()
            };
            let _ = self.send_phone_locked(
                &link,
                &request,
                &uuid::Uuid::new_v4().to_string(),
                &self.phone_data(),
            );
        }
        Ok(())
    }
    pub fn register_phone(&self, token: &str, name: &str, server: Option<&str>) -> io::Result<()> {
        let fallback = self
            .relay_config()?
            .map_or_else(|| DEFAULT_BRIDGE_SERVER.to_owned(), |config| config.server);
        let link = PhoneLink::new(name, token, server.unwrap_or(&fallback))?;
        self.cancel_phone_pairing()?;
        self.phone_links
            .lock()
            .map_err(poisoned)?
            .as_mut()
            .ok_or_else(unavailable)?
            .register(link)
    }
    pub fn remove_phone(&self, id: &str) -> io::Result<()> {
        self.phone_links
            .lock()
            .map_err(poisoned)?
            .as_mut()
            .ok_or_else(unavailable)?
            .remove(id)
    }
    pub fn cancel_phone_pairing(&self) -> io::Result<()> {
        if let Some(pairing) = self.phone_pairing.lock().map_err(poisoned)?.as_mut() {
            pairing.cancel.store(true, Ordering::Release);
            if pairing.view.state == "awaiting" {
                pairing.view.state = "cancelled".into();
                pairing.view.message = None;
            }
        }
        Ok(())
    }
    pub fn begin_phone_pairing(&self, server: Option<&str>) -> io::Result<()> {
        if self.phone_links.lock().map_err(poisoned)?.is_none() {
            return Err(unavailable());
        }
        let relay = self.relay_config()?;
        let fallback = relay
            .as_ref()
            .map_or(DEFAULT_BRIDGE_SERVER, |config| config.server.as_str());
        let server = sidepulse_relay::normalize_server(server.unwrap_or(fallback))?;
        let sender = relay
            .as_ref()
            .map_or("Remote computer", |config| config.machine_name.as_str());
        let channel = sidepulse_links::new_pairing_channel()?;
        let url = sidepulse_links::pairing_url(&server, &channel, sender)?;
        let qr = sidepulse_links::qr_matrix(&url)?;
        self.cancel_phone_pairing()?;
        let cancel = Arc::new(AtomicBool::new(false));
        *self.phone_pairing.lock().map_err(poisoned)? = Some(PairingRuntime {
            view: PhonePairingView {
                id: channel.clone(),
                url,
                state: "awaiting".into(),
                expires_at: Utc::now() + chrono::Duration::minutes(5),
                qr,
                message: None,
            },
            cancel: cancel.clone(),
        });
        let service = self.clone();
        std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(300);
            while service.running() && Instant::now() < deadline && !cancel.load(Ordering::Acquire)
            {
                let result = sidepulse_links::receive_registration_once(
                    &server,
                    &channel,
                    deadline.saturating_duration_since(Instant::now()),
                    || {
                        service.running()
                            && !cancel.load(Ordering::Acquire)
                            && Instant::now() < deadline
                    },
                );
                if !service.running() || cancel.load(Ordering::Acquire) {
                    return;
                }
                let Ok(mut pairing_guard) = service.phone_pairing.lock() else {
                    return;
                };
                let Some(pairing) = pairing_guard.as_mut().filter(|pairing| {
                    pairing.view.id == channel && pairing.view.state == "awaiting"
                }) else {
                    return;
                };
                if Instant::now() >= deadline {
                    pairing.view.state = "expired".into();
                    pairing.view.message = Some("Pairing timed out. Try again.".into());
                    return;
                }
                match result {
                    Ok(Some(link)) => {
                        let result =
                            service
                                .phone_links
                                .lock()
                                .map_err(poisoned)
                                .and_then(|mut store| {
                                    store.as_mut().ok_or_else(unavailable)?.register(link)
                                });
                        pairing.view.state =
                            if result.is_ok() { "linked" } else { "failed" }.into();
                        pairing.view.message = result.err().map(|error| error.to_string());
                        return;
                    }
                    Ok(None) => pairing.view.message = None,
                    Err(error) => {
                        pairing.view.message = Some(format!("Waiting to reconnect: {error}"))
                    }
                }
                drop(pairing_guard);
                std::thread::sleep(Duration::from_millis(500));
            }
            if let Ok(mut pairing) = service.phone_pairing.lock()
                && let Some(pairing) = pairing.as_mut().filter(|pairing| {
                    pairing.view.id == channel && pairing.view.state == "awaiting"
                })
            {
                pairing.view.state = if cancel.load(Ordering::Acquire) {
                    "cancelled"
                } else {
                    "expired"
                }
                .into();
            }
        });
        Ok(())
    }

    fn delivery_plan(
        &self,
        request: &DeliveryRequest,
    ) -> io::Result<(DeliveryRequest, Vec<DeliveryDestination>, Vec<PhoneLink>)> {
        let mut request = request.clone();
        if let Some(program) = &request.program {
            let program = sidepulse_device::normalize_led_text(program);
            sidepulse_device::validate_led_text(&program)?;
            sidepulse_device::led_runtime::validate_program(&program, 8)?;
            request.program = Some(program);
        }
        request
            .validate()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        let links = self
            .phone_links
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .map_or_else(Vec::new, PhoneStore::links);
        let devices = if request.device.is_some() {
            Vec::new()
        } else {
            self.available_devices()?.0
        };
        let candidates = devices
            .into_iter()
            .map(|device| sidepulse_device::DeviceCandidate {
                root: device.root.into(),
                target: device.target.into(),
                reason: device.reason,
                label: device.label,
            })
            .collect();
        let mut destinations =
            sidepulse_device::delivery::destinations_from_devices(&request, candidates)?;
        if request.device.is_none() {
            destinations.extend(links.iter().map(|link| {
                let summary = link.summary();
                DeliveryDestination {
                    id: summary.id.clone(),
                    name: summary.name,
                    kind: DestinationKind::Phone,
                    address: summary.id,
                    aliases: vec![],
                }
            }));
        }
        let selected = sidepulse_core::select_delivery_targets(&request, &destinations)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        Ok((request, selected, links))
    }
    pub fn start_delivery(&self, request: &DeliveryRequest) -> io::Result<ServerPayload> {
        let (request, selected, links) = self.delivery_plan(request)?;
        let outcomes: Vec<_> = selected
            .iter()
            .map(|destination| DeliveryOutcome {
                destination: destination.clone(),
                status: if request.dry_run {
                    "planned"
                } else {
                    "pending"
                }
                .into(),
                error: None,
            })
            .collect();
        if request.dry_run {
            return Ok(ServerPayload::Delivery { outcomes });
        }
        let id = uuid::Uuid::new_v4().to_string();
        let job = DeliveryJobView {
            id: id.clone(),
            state: "running".into(),
            outcomes,
        };
        let mut jobs = self.delivery_jobs.lock().map_err(poisoned)?;
        if jobs.values().filter(|job| job.state == "running").count() >= 16 {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "too many deliveries are in progress",
            ));
        }
        if jobs.len() >= 64 {
            let old = jobs
                .iter()
                .find(|(_, job)| job.state != "running")
                .map(|(id, _)| id.clone());
            if let Some(old) = old {
                jobs.remove(&old);
            }
        }
        jobs.insert(id.clone(), job.clone());
        drop(jobs);
        let service = self.clone();
        std::thread::spawn(move || {
            let event_id = uuid::Uuid::new_v4().to_string();
            let data = service.phone_data();
            for (index, destination) in selected.iter().enumerate() {
                if !service.running() {
                    if let Ok(mut jobs) = service.delivery_jobs.lock()
                        && let Some(job) = jobs.get_mut(&id)
                    {
                        for outcome in &mut job.outcomes[index..] {
                            outcome.status = "failed".into();
                            outcome.error = Some("service is stopping".into());
                        }
                    }
                    break;
                }
                let result = match destination.kind {
                    DestinationKind::Local => {
                        service.deliver_local(destination, request.program.as_deref().unwrap())
                    }
                    DestinationKind::Phone => {
                        let matching: Vec<_> = links
                            .iter()
                            .filter(|link| link.summary().id == destination.id)
                            .collect();
                        if matching.len() != 1 {
                            Err(io::Error::new(
                                io::ErrorKind::InvalidInput,
                                "phone identifier is missing or ambiguous",
                            ))
                        } else {
                            service.deliver_phone(matching[0], &request, &event_id, &data)
                        }
                    }
                };
                if let Ok(mut jobs) = service.delivery_jobs.lock()
                    && let Some(job) = jobs.get_mut(&id)
                {
                    job.outcomes[index].status = if result.is_ok() {
                        if destination.kind == DestinationKind::Phone {
                            "sent"
                        } else {
                            "written"
                        }
                    } else {
                        "failed"
                    }
                    .into();
                    job.outcomes[index].error = result.err().map(|error| error.to_string());
                }
            }
            if let Ok(mut jobs) = service.delivery_jobs.lock()
                && let Some(job) = jobs.get_mut(&id)
            {
                job.state = if job.outcomes.iter().any(|outcome| outcome.error.is_some()) {
                    "failed"
                } else {
                    "completed"
                }
                .into();
            }
        });
        Ok(ServerPayload::DeliveryJob { job })
    }
    pub fn delivery_status(&self, id: &str) -> io::Result<ServerPayload> {
        self.delivery_jobs
            .lock()
            .map_err(poisoned)?
            .get(id)
            .cloned()
            .map(|job| ServerPayload::DeliveryJob { job })
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "delivery is no longer available")
            })
    }
    fn deliver_local(&self, destination: &DeliveryDestination, program: &str) -> io::Result<()> {
        let path = Path::new(&destination.address);
        sidepulse_device::led_runtime::validate_program(
            program,
            sidepulse_device::led_count_for_target(path),
        )?;
        let root = path.parent().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "device target has no parent")
        })?;
        if !root.is_dir() {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "device is not mounted",
            ));
        }
        if path.is_dir() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "device target is a directory",
            ));
        }
        let mut device = self.device.lock().map_err(poisoned)?;
        let mut settings = self.settings.lock().map_err(poisoned)?;
        let brightness = settings
            .as_ref()
            .map_or(255, |store| store.brightness_for_device(root));
        if let Some(store) = settings.as_mut() {
            if store.display_for_device(root) != "custom" {
                store.set_display_for_device(root, "custom", brightness)?;
            }
        } else if device
            .as_ref()
            .is_some_and(|device| device.target() == path)
        {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "manual output requires a configured settings file",
            ));
        }
        if let Some(device) = device.as_mut().filter(|device| device.target() == path) {
            device.sync_program(program)?;
        } else {
            sidepulse_device::DeviceOutput::with_target(path, brightness).sync_program(program)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};
    use std::{
        io::{BufRead, BufReader, Read, Write},
        net::TcpListener,
        sync::mpsc,
    };

    fn mock_phone(
        count: usize,
        status: &'static str,
    ) -> (String, mpsc::Receiver<Value>, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let server = format!("http://{}", listener.local_addr().unwrap());
        let (sent, received) = mpsc::channel();
        let worker = std::thread::spawn(move || {
            for _ in 0..count {
                let (stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                let mut reader = BufReader::new(stream);
                let mut first = String::new();
                reader.read_line(&mut first).unwrap();
                assert!(first.starts_with("POST /api/leds/apns_"));
                let mut size = 0;
                loop {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    if line == "\r\n" {
                        break;
                    }
                    if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                        size = value.trim().parse().unwrap();
                    }
                }
                let mut body = vec![0; size];
                reader.read_exact(&mut body).unwrap();
                sent.send(serde_json::from_slice(&body).unwrap()).unwrap();
                write!(
                    reader.get_mut(),
                    "HTTP/1.1 {status}\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                )
                .unwrap();
            }
        });
        (server, received, worker)
    }
    fn service_with_phone(server: &str) -> (Service, tempfile::TempDir) {
        let dir = tempfile::tempdir().unwrap();
        let settings = dir.path().join("settings.json");
        std::fs::write(&settings,json!({"unknown":7,"battery_monitoring":{"show_on_power_change":false},"devices":[{"id":"ios/abababababab","path":"ios/abababababab","extra":true,"led_display":"agent"}]}).to_string()).unwrap();
        let service = Service::new();
        service.configure_settings(&settings).unwrap();
        service
            .configure_phone_links(&dir.path().join("links.json"))
            .unwrap();
        service
            .register_phone(&"ab".repeat(32), "Test phone", Some(server))
            .unwrap();
        service.enable_phone_output().unwrap();
        (service, dir)
    }
    #[test]
    fn automatic_phone_updates_deduplicate_and_respect_manual_and_battery_display() {
        let (server, received, worker) = mock_phone(4, "200 OK");
        let (service, dir) = service_with_phone(&server);
        service.sync_phone_outputs().unwrap();
        let idle = received.recv_timeout(Duration::from_secs(3)).unwrap();
        assert!(idle["leds"].as_str().unwrap().contains("#006060"));
        service.sync_phone_outputs().unwrap();
        assert!(received.try_recv().is_err());
        let ServerPayload::DeliveryJob { job } = service
            .start_delivery(&DeliveryRequest {
                program: Some("off".into()),
                prefer_phone: true,
                ..Default::default()
            })
            .unwrap()
        else {
            panic!("missing job")
        };
        let started = Instant::now();
        loop {
            let ServerPayload::DeliveryJob { job } = service.delivery_status(&job.id).unwrap()
            else {
                panic!("missing job")
            };
            if job.state == "completed" {
                break;
            }
            assert!(started.elapsed() < Duration::from_secs(3));
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(
            received.recv_timeout(Duration::from_secs(3)).unwrap()["leds"],
            "off"
        );
        service.sync_phone_outputs().unwrap();
        assert!(received.try_recv().is_err());
        let ServerPayload::PhoneLinks {
            links,
            output_enabled,
            ..
        } = service.phone_links_snapshot().unwrap()
        else {
            panic!("missing links")
        };
        assert!(output_enabled);
        assert_eq!(links[0].display, "custom");
        assert!(links[0].last_sent_at.is_some());
        service.set_phone_display("abababababab", "agent").unwrap();
        let event=sidepulse_core::parse_log_line("codex",&json!({"hook_event_name":"UserPromptSubmit","session_id":"test","timestamp":Utc::now().to_rfc3339()}).to_string()).unwrap();
        service.ingest_record(&event).unwrap();
        service.sync_phone_outputs().unwrap();
        let working = received.recv_timeout(Duration::from_secs(3)).unwrap();
        assert!(working["leds"].as_str().unwrap().contains("#00E5FF"));
        service
            .set_phone_display("abababababab", "battery")
            .unwrap();
        let battery = sidepulse_device::battery::BatteryState {
            percent: 50,
            ..Default::default()
        };
        service.sync_device_with_battery(Some(battery)).unwrap();
        service.sync_phone_outputs().unwrap();
        let payload = received.recv_timeout(Duration::from_secs(3)).unwrap();
        assert_eq!(
            payload["leds"],
            sidepulse_device::battery::program_for_battery(battery, 8, 360, 255)
        );
        worker.join().unwrap();
        let settings: Value =
            serde_json::from_slice(&std::fs::read(dir.path().join("settings.json")).unwrap())
                .unwrap();
        assert_eq!(settings["unknown"], 7);
        assert_eq!(settings["devices"][0]["extra"], true);
        assert_eq!(settings["devices"][0]["led_display"], "battery");
        service.remove_phone("abababababab").unwrap();
        service.sync_phone_outputs().unwrap();
        assert!(service.phone_output.lock().unwrap().targets.is_empty());
    }
    #[test]
    fn failed_phone_updates_report_redacted_error_and_back_off() {
        let (server, received, worker) = mock_phone(1, "500 Internal Server Error");
        let (service, _dir) = service_with_phone(&server);
        service.sync_phone_outputs().unwrap();
        received.recv_timeout(Duration::from_secs(3)).unwrap();
        worker.join().unwrap();
        service.sync_phone_outputs().unwrap();
        let ServerPayload::PhoneLinks { links, .. } = service.phone_links_snapshot().unwrap()
        else {
            panic!("missing links")
        };
        assert!(links[0].last_sent_at.is_none());
        let error = links[0].delivery_error.as_ref().unwrap();
        assert!(!error.contains(&"ab".repeat(32)));
        assert!(error.contains("500"));
    }
}
