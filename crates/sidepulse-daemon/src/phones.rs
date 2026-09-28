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
    io,
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

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
        let links = store.as_ref().map_or_else(Vec::new, |store| {
            store.links().iter().map(PhoneLink::summary).collect()
        });
        let configured = store.is_some();
        drop(store);
        let pairing = self
            .phone_pairing
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .map(|pairing| pairing.view.clone());
        Ok(ServerPayload::PhoneLinks {
            configured,
            links,
            pairing,
        })
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
            while Instant::now() < deadline && !cancel.load(Ordering::Acquire) {
                let result = sidepulse_links::receive_registration_once(
                    &server,
                    &channel,
                    deadline.saturating_duration_since(Instant::now()),
                    || !cancel.load(Ordering::Acquire) && Instant::now() < deadline,
                );
                if cancel.load(Ordering::Acquire) {
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
        let destinations = if let Some(path) = &request.device {
            let path = Path::new(path);
            let target = if path.file_name().is_some_and(|name| {
                name.to_string_lossy()
                    .eq_ignore_ascii_case(sidepulse_device::DEFAULT_FILE_NAME)
            }) {
                path.to_owned()
            } else {
                path.join(
                    request
                        .file_name
                        .as_deref()
                        .unwrap_or(sidepulse_device::DEFAULT_FILE_NAME),
                )
            };
            vec![DeliveryDestination {
                id: target.to_string_lossy().into(),
                name: path
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .into(),
                kind: DestinationKind::Local,
                address: target.to_string_lossy().into(),
                aliases: vec![],
            }]
        } else {
            let (devices, _) = self.available_devices()?;
            let mut destinations: Vec<_> = devices
                .into_iter()
                .map(|device| DeliveryDestination {
                    id: device.root.clone(),
                    name: device.label.unwrap_or_else(|| {
                        Path::new(&device.root)
                            .file_name()
                            .unwrap_or_default()
                            .to_string_lossy()
                            .into()
                    }),
                    kind: DestinationKind::Local,
                    address: if let Some(file) = &request.file_name {
                        Path::new(&device.root).join(file).to_string_lossy().into()
                    } else {
                        device.target.clone()
                    },
                    aliases: vec![device.root, device.target],
                })
                .collect();
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
            destinations
        };
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
            let source = service.relay_config().ok().flatten().map_or_else(
                || "Remote computer".to_owned(),
                |config| config.machine_name,
            );
            let mut data = serde_json::json!({"source":{"name":source}});
            if let Ok(battery) = service.battery_diagnostics.lock()
                && let Some(battery) = battery.as_ref().filter(|battery| battery.battery_present)
            {
                data["source"]["battery"] = serde_json::json!({"level":battery.percent,"charging":battery.is_charging,"plugged_in":battery.is_plugged});
            }
            for (index, destination) in selected.iter().enumerate() {
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
                            sidepulse_links::send_program(
                                matching[0],
                                request.program.as_deref(),
                                request.title.as_deref(),
                                request.message.as_deref(),
                                &event_id,
                                &data,
                            )
                            .map(|_| ())
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
            store.set_display_for_device(root, "custom", brightness)?;
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
