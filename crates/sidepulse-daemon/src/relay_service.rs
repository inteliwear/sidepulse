//! Service ownership of relay configuration; UI clients never write relay.json.

use super::{Service, poisoned};
use sidepulse_core::{RelaySettings, RelaySettingsPatch};
use sidepulse_relay::{RelayConfig, RelayConfigStore, clean_channel, normalize_server};
use std::{io, path::Path, sync::atomic::Ordering};

#[derive(Default)]
pub(super) struct RelayHealth {
    pub last_received_at: Option<chrono::DateTime<chrono::Utc>>,
    pub last_sent_at: Option<chrono::DateTime<chrono::Utc>>,
    pub receive_error: Option<String>,
    pub send_error: Option<String>,
}
fn host() -> String {
    std::env::var("HOSTNAME")
        .or_else(|_| std::env::var("COMPUTERNAME"))
        .unwrap_or_else(|_| "Remote computer".into())
}
impl Service {
    pub fn configure_relay(&self, path: &Path) -> io::Result<()> {
        let store = RelayConfigStore::load(path, &host())?;
        *self.relay_config_store.lock().map_err(poisoned)? = Some(store);
        self.relay_generation.fetch_add(1, Ordering::AcqRel);
        Ok(())
    }
    pub(super) fn relay_config(&self) -> io::Result<Option<RelayConfig>> {
        Ok(self
            .relay_config_store
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .map(|store| store.config().clone()))
    }
    pub fn relay_settings(&self) -> io::Result<RelaySettings> {
        let config = self.relay_config()?;
        let configured = config.is_some();
        let config = config.unwrap_or_else(|| RelayConfig::default_for_host(&host()));
        let health = self.relay_health.lock().map_err(poisoned)?;
        Ok(RelaySettings {
            configured,
            server: config.server,
            machine_name: config.machine_name,
            receiver_code: config.receiver_channel,
            outbound_code: config.outbound_channel,
            last_received_at: health.last_received_at,
            last_sent_at: health.last_sent_at,
            receive_error: health.receive_error.clone(),
            send_error: health.send_error.clone(),
        })
    }
    pub fn set_relay_settings(&self, patch: &RelaySettingsPatch) -> io::Result<()> {
        patch
            .validate()
            .map_err(|message| io::Error::new(io::ErrorKind::InvalidInput, message))?;
        let mut store = self.relay_config_store.lock().map_err(poisoned)?;
        let store = store.as_mut().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "no relay settings path is configured",
            )
        })?;
        let mut config = store.config().clone();
        if let Some(server) = &patch.server {
            config.server = normalize_server(server)?;
        }
        if let Some(name) = &patch.machine_name {
            if name.trim().is_empty() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "computer name is required",
                ));
            }
            config.machine_name = name.trim().chars().take(80).collect();
        }
        if let Some(code) = &patch.outbound_code {
            config.outbound_channel = clean_channel(code)?;
        }
        if patch.rotate_receiver {
            config.receiver_channel.clear();
        }
        if patch.receiver_enabled == Some(false) {
            config.receiver_channel.clear();
        } else if patch.receiver_enabled == Some(true) || patch.rotate_receiver {
            config = config.with_receiver_channel()?;
        }
        store.save(config)?;
        self.relay_generation.fetch_add(1, Ordering::AcqRel);
        *self.relay_health.lock().map_err(poisoned)? = RelayHealth::default();
        Ok(())
    }
    pub fn reload_relay_settings(&self) -> io::Result<()> {
        let path = self
            .relay_config_store
            .lock()
            .map_err(poisoned)?
            .as_ref()
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::NotFound,
                    "no relay settings path is configured",
                )
            })?
            .path()
            .to_owned();
        self.configure_relay(&path)
    }
}
