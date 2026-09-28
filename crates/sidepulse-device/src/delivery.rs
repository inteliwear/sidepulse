//! Local destination resolution shared by detached CLI tools and the service.
use sidepulse_core::{DeliveryDestination, DeliveryRequest, DestinationKind};
use std::{
    io,
    path::{Path, PathBuf},
};
pub fn local_destinations(
    request: &DeliveryRequest,
    roots: &[PathBuf],
) -> io::Result<Vec<DeliveryDestination>> {
    destinations_from_devices(
        request,
        if request.device.is_some() {
            Vec::new()
        } else {
            crate::discover_devices(roots)
        },
    )
}
pub fn destinations_from_devices(
    request: &DeliveryRequest,
    devices: Vec<crate::DeviceCandidate>,
) -> io::Result<Vec<DeliveryDestination>> {
    request
        .validate()
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
    if let Some(path) = &request.device {
        let path = Path::new(path);
        let target = if path.file_name().is_some_and(|name| {
            name.to_string_lossy()
                .eq_ignore_ascii_case(crate::DEFAULT_FILE_NAME)
        }) {
            path.to_path_buf()
        } else {
            path.join(
                request
                    .file_name
                    .as_deref()
                    .unwrap_or(crate::DEFAULT_FILE_NAME),
            )
        };
        return Ok(vec![DeliveryDestination {
            id: target.to_string_lossy().into(),
            name: path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .into(),
            address: target.to_string_lossy().into(),
            kind: DestinationKind::Local,
            aliases: vec![],
        }]);
    }
    Ok(devices
        .into_iter()
        .map(|device| DeliveryDestination {
            id: device.root.to_string_lossy().into(),
            name: device.label.clone().unwrap_or_else(|| {
                device
                    .root
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .into()
            }),
            kind: DestinationKind::Local,
            address: request
                .file_name
                .as_ref()
                .map_or(device.target.to_string_lossy().into_owned(), |file| {
                    device.root.join(file).to_string_lossy().into_owned()
                }),
            aliases: vec![
                device.root.to_string_lossy().into(),
                device.target.to_string_lossy().into(),
            ],
        })
        .collect())
}
