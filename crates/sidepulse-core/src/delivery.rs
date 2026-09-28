//! Destination selection and phone presentation data, without I/O or UI code.
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DestinationKind {
    Local,
    Phone,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeliveryDestination {
    pub id: String,
    pub name: String,
    pub kind: DestinationKind,
    pub address: String,
    #[serde(default)]
    pub aliases: Vec<String>,
}
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeliveryRequest {
    pub program: Option<String>,
    pub title: Option<String>,
    pub message: Option<String>,
    pub requested: Option<String>,
    #[serde(default)]
    pub send_all: bool,
    #[serde(default)]
    pub prefer_phone: bool,
    #[serde(default)]
    pub dry_run: bool,
    pub device: Option<String>,
    pub file_name: Option<String>,
}
impl DeliveryRequest {
    pub fn has_notification(&self) -> bool {
        [&self.title, &self.message]
            .into_iter()
            .flatten()
            .any(|text| !text.trim().is_empty())
    }
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.requested.is_some() && self.send_all {
            return Err("use either --to or --all");
        }
        if self.device.is_some() && (self.requested.is_some() || self.send_all) {
            return Err("--device cannot be combined with --to or --all");
        }
        if self.device.is_some() && self.has_notification() {
            return Err("a local device cannot display notifications");
        }
        if self.program.is_none() && !self.has_notification() {
            return Err("provide an LED program, title, or message");
        }
        if self
            .program
            .as_ref()
            .is_some_and(|text| text.is_empty() || text.len() > 65536 || text.contains('\0'))
            || [&self.title, &self.message]
                .into_iter()
                .flatten()
                .any(|text| text.len() > 16384 || text.contains('\0'))
        {
            return Err("delivery content is empty or too large");
        }
        if self.file_name.as_ref().is_some_and(|name| {
            name.is_empty()
                || name.len() > 255
                || name.contains(['/', '\\', '\0'])
                || matches!(name.as_str(), "." | "..")
        }) {
            return Err("invalid LED file name");
        }
        if self
            .device
            .as_ref()
            .is_some_and(|path| path.len() > 4096 || path.contains('\0'))
            || self
                .requested
                .as_ref()
                .is_some_and(|target| target.len() > 4096)
        {
            return Err("invalid delivery destination");
        }
        Ok(())
    }
}

pub fn select_delivery_targets(
    request: &DeliveryRequest,
    destinations: &[DeliveryDestination],
) -> Result<Vec<DeliveryDestination>, String> {
    request.validate().map_err(str::to_owned)?;
    let phones = || {
        destinations
            .iter()
            .filter(|destination| destination.kind == DestinationKind::Phone)
    };
    let locals = || {
        destinations
            .iter()
            .filter(|destination| destination.kind == DestinationKind::Local)
    };
    if request.send_all {
        if request.has_notification() && phones().next().is_none() {
            return Err("no linked phone can display the notification".into());
        }
        let selected: Vec<_> = destinations
            .iter()
            .filter(|destination| {
                destination.kind == DestinationKind::Phone || request.program.is_some()
            })
            .cloned()
            .collect();
        if selected.is_empty() {
            return Err("no destinations found".into());
        }
        return Ok(selected);
    }
    let selected: Vec<_> = if let Some(requested) = &request.requested {
        let query = requested.trim().to_lowercase();
        destinations
            .iter()
            .filter(|destination| {
                (query == "local" && destination.kind == DestinationKind::Local)
                    || (query == "phone" && destination.kind == DestinationKind::Phone)
                    || query == destination.id.to_lowercase()
                    || query == destination.name.to_lowercase()
                    || query == destination.address.to_lowercase()
                    || destination
                        .aliases
                        .iter()
                        .any(|alias| query == alias.to_lowercase())
                    || (destination.kind == DestinationKind::Phone
                        && query.len() >= 4
                        && destination.id.to_lowercase().starts_with(&query))
            })
            .cloned()
            .collect()
    } else {
        let choose_phones = request.has_notification()
            || (request.prefer_phone && phones().next().is_some())
            || locals().next().is_none();
        destinations
            .iter()
            .filter(|destination| (destination.kind == DestinationKind::Phone) == choose_phones)
            .cloned()
            .collect()
    };
    if selected.is_empty() {
        return Err(
            "no matching destination found; link a phone or select a mounted device".into(),
        );
    }
    if selected.len() > 1 {
        return Err(format!(
            "destination is ambiguous; choose an ID with --to, or use --all:\n{}",
            selected
                .iter()
                .map(|destination| format!("  {} ({})", destination.name, destination.id))
                .collect::<Vec<_>>()
                .join("\n")
        ));
    }
    if selected[0].kind == DestinationKind::Local
        && (request.has_notification() || request.program.is_none())
    {
        return Err(
            "a local device requires an LED program and cannot display notifications".into(),
        );
    }
    Ok(selected)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PhoneLinkSummary {
    pub id: String,
    pub name: String,
    pub server: String,
    pub linked_at: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PhonePairingView {
    pub id: String,
    pub url: String,
    pub state: String,
    pub expires_at: chrono::DateTime<chrono::Utc>,
    pub qr: Vec<Vec<bool>>,
    pub message: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeliveryOutcome {
    pub destination: DeliveryDestination,
    pub status: String,
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeliveryJobView {
    pub id: String,
    pub state: String,
    pub outcomes: Vec<DeliveryOutcome>,
}

#[cfg(test)]
mod tests {
    use super::*;
    fn targets() -> Vec<DeliveryDestination> {
        vec![
            DeliveryDestination {
                id: "local-id".into(),
                name: "Stick".into(),
                kind: DestinationKind::Local,
                address: "/Volumes/STICK/agent.LED".into(),
                aliases: vec!["/Volumes/STICK".into()],
            },
            DeliveryDestination {
                id: "abcdef012345".into(),
                name: "Phone".into(),
                kind: DestinationKind::Phone,
                address: "abcdef012345".into(),
                aliases: vec![],
            },
        ]
    }
    #[test]
    fn python_write_push_all_and_ambiguity_policy() {
        let destinations = targets();
        let mut request = DeliveryRequest {
            program: Some("off".into()),
            ..Default::default()
        };
        assert_eq!(
            select_delivery_targets(&request, &destinations).unwrap()[0].kind,
            DestinationKind::Local
        );
        request.prefer_phone = true;
        assert_eq!(
            select_delivery_targets(&request, &destinations).unwrap()[0].kind,
            DestinationKind::Phone
        );
        request.send_all = true;
        assert_eq!(
            select_delivery_targets(&request, &destinations)
                .unwrap()
                .len(),
            2
        );
        request.program = None;
        request.title = Some("Done".into());
        assert_eq!(
            select_delivery_targets(&request, &destinations)
                .unwrap()
                .len(),
            1
        );
        request.send_all = false;
        request.requested = Some("local".into());
        assert!(
            select_delivery_targets(&request, &destinations)
                .unwrap_err()
                .contains("cannot display notifications")
        );
        request.requested = Some("abcd".into());
        assert_eq!(
            select_delivery_targets(&request, &destinations).unwrap()[0].id,
            "abcdef012345"
        );
        let mut ambiguous = destinations.clone();
        ambiguous.push(destinations[1].clone());
        assert!(
            select_delivery_targets(&request, &ambiguous)
                .unwrap_err()
                .contains("ambiguous")
        );
        request.requested = None;
        request.send_all = true;
        assert!(select_delivery_targets(&request, &destinations[..1]).is_err());
    }
}
