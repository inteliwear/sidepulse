//! Portable local transport for the versioned SidePulse protocol.

use std::io::{self, BufRead, BufReader, Read, Write};
use std::time::Duration;

use interprocess::local_socket::prelude::*;
use interprocess::local_socket::{self, ListenerOptions};
use serde::{Serialize, de::DeserializeOwned};

pub const MAX_MESSAGE_BYTES: usize = 1024 * 1024;

/// A socket pathname on Unix, or a local named-pipe identifier on Windows.
pub fn bind(endpoint: &str) -> io::Result<local_socket::Listener> {
    let name = endpoint_name(endpoint)?;
    let options = ListenerOptions::new().name(name);
    #[cfg(target_os = "linux")]
    let options = {
        use interprocess::os::unix::local_socket::ListenerOptionsExt;
        options.mode(0o600)
    };
    let listener = options.create_sync()?;
    #[cfg(target_os = "macos")]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(endpoint, std::fs::Permissions::from_mode(0o600))?;
    }
    Ok(listener)
}

pub fn connect(endpoint: &str, timeout: Duration) -> io::Result<local_socket::Stream> {
    let stream = local_socket::Stream::connect(endpoint_name(endpoint)?)?;
    stream.set_send_timeout(Some(timeout))?;
    stream.set_recv_timeout(Some(timeout))?;
    Ok(stream)
}

pub fn write_message<T: Serialize>(stream: &mut impl Write, value: &T) -> io::Result<()> {
    let message = serde_json::to_vec(value).map_err(io::Error::other)?;
    if message.len() + 1 > MAX_MESSAGE_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "IPC message too large",
        ));
    }
    stream.write_all(&message)?;
    stream.write_all(b"\n")?;
    stream.flush()
}

pub fn read_message<T: DeserializeOwned>(stream: &mut impl BufRead) -> io::Result<T> {
    let mut bytes = Vec::new();
    stream
        .take((MAX_MESSAGE_BYTES + 1) as u64)
        .read_until(b'\n', &mut bytes)?;
    if bytes.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "IPC peer closed",
        ));
    }
    if bytes.len() > MAX_MESSAGE_BYTES || !bytes.ends_with(b"\n") {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "IPC message exceeds limit or has no terminator",
        ));
    }
    serde_json::from_slice(&bytes[..bytes.len() - 1])
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

pub fn request<T: Serialize, R: DeserializeOwned>(
    endpoint: &str,
    value: &T,
    timeout: Duration,
) -> io::Result<R> {
    let mut stream = connect(endpoint, timeout)?;
    write_message(&mut stream, value)?;
    read_message(&mut BufReader::new(stream))
}

#[cfg(unix)]
fn endpoint_name(endpoint: &str) -> io::Result<local_socket::Name<'_>> {
    use interprocess::local_socket::{GenericFilePath, ToFsName};
    endpoint.to_fs_name::<GenericFilePath>()
}

#[cfg(windows)]
fn endpoint_name(endpoint: &str) -> io::Result<local_socket::Name<'_>> {
    use interprocess::local_socket::{GenericNamespaced, ToNsName};
    endpoint.to_ns_name::<GenericNamespaced>()
}

#[cfg(test)]
mod tests {
    use super::*;
    use sidepulse_core::{
        ClientRequest, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload,
    };

    #[cfg(unix)]
    #[test]
    fn round_trips_a_snapshot_request_over_local_socket() {
        use std::os::unix::fs::PermissionsExt;
        let directory = std::path::Path::new("/tmp").join(format!(
            "sidepulse-ipc-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&directory).unwrap();
        std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700)).unwrap();
        let endpoint = directory.join("service.sock");
        let endpoint = endpoint.to_str().unwrap().to_owned();
        let listener = bind(&endpoint).unwrap();
        let server = std::thread::spawn(move || {
            let mut stream = listener.accept().unwrap();
            let request: ClientRequest = read_message(&mut BufReader::new(&mut stream)).unwrap();
            assert_eq!(request.validate(), Ok(()));
            assert_eq!(request.kind, RequestKind::Snapshot);
            write_message(
                &mut stream,
                &ServerMessage {
                    version: PROTOCOL_VERSION,
                    request_id: Some(request.request_id),
                    payload: ServerPayload::Ack,
                },
            )
            .unwrap();
        });
        let response: ServerMessage = request(
            &endpoint,
            &ClientRequest {
                version: PROTOCOL_VERSION,
                request_id: 7,
                kind: RequestKind::Snapshot,
            },
            Duration::from_secs(1),
        )
        .unwrap();
        assert_eq!(response.request_id, Some(7));
        assert_eq!(response.payload, ServerPayload::Ack);
        server.join().unwrap();
        std::fs::remove_dir(&directory).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn round_trips_a_snapshot_request_over_named_pipe() {
        let endpoint = format!("sidepulse-ipc-test-{}", std::process::id());
        let listener = bind(&endpoint).unwrap();
        let server = std::thread::spawn(move || {
            let mut stream = listener.accept().unwrap();
            let request: ClientRequest = read_message(&mut BufReader::new(&mut stream)).unwrap();
            assert_eq!(request.kind, RequestKind::Snapshot);
            write_message(
                &mut stream,
                &ServerMessage {
                    version: PROTOCOL_VERSION,
                    request_id: Some(request.request_id),
                    payload: ServerPayload::Ack,
                },
            )
            .unwrap();
        });
        let response: ServerMessage = request(
            &endpoint,
            &ClientRequest {
                version: PROTOCOL_VERSION,
                request_id: 7,
                kind: RequestKind::Snapshot,
            },
            Duration::from_secs(1),
        )
        .unwrap();
        assert_eq!(response.payload, ServerPayload::Ack);
        server.join().unwrap();
    }
}
