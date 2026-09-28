//! Relay connection settings and SSE framing, shared by CLI and service.

use std::fs;
#[cfg(test)]
use std::io::Write;
use std::io::{self, BufRead, BufReader, Read};
use std::path::Path;
use std::time::Duration;

use base64::Engine;
use serde_json::{Value, json};
mod store;
pub use store::RelayConfigStore;
use url::Url;

pub const DEFAULT_BRIDGE_SERVER: &str = "https://bridge.sidepulse.io";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RelayConfig {
    pub server: String,
    pub receiver_channel: String,
    pub outbound_channel: String,
    pub machine_name: String,
}

impl RelayConfig {
    pub fn default_for_host(machine_name: &str) -> Self {
        Self {
            server: DEFAULT_BRIDGE_SERVER.into(),
            receiver_channel: String::new(),
            outbound_channel: String::new(),
            machine_name: clean_machine_name(machine_name),
        }
    }

    pub fn from_legacy_json(document: &Value, machine_name: &str) -> Self {
        let mut result = Self::default_for_host(machine_name);
        if document.get("version").and_then(Value::as_u64) != Some(1) {
            return result;
        }
        if let Some(server) = document.get("server").and_then(Value::as_str)
            && let Ok(normalized) = normalize_server(server)
        {
            result.server = normalized;
        }
        result.receiver_channel = document
            .get("receiver_channel")
            .and_then(Value::as_str)
            .and_then(|value| clean_channel(value).ok())
            .unwrap_or_default();
        result.outbound_channel = document
            .get("outbound_channel")
            .and_then(Value::as_str)
            .and_then(|value| clean_channel(value).ok())
            .unwrap_or_default();
        if let Some(name) = document.get("machine_name").and_then(Value::as_str)
            && !name.trim().is_empty()
        {
            result.machine_name = clean_machine_name(name);
        }
        result
    }

    pub fn to_legacy_json(&self) -> io::Result<Value> {
        Ok(json!({
            "version": 1,
            "server": normalize_server(&self.server)?,
            "receiver_channel": clean_channel(&self.receiver_channel)?,
            "outbound_channel": clean_channel(&self.outbound_channel)?,
            "machine_name": clean_machine_name(&self.machine_name),
        }))
    }

    pub fn with_receiver_channel(mut self) -> io::Result<Self> {
        if self.receiver_channel.is_empty() {
            let mut bytes = [0_u8; 16];
            getrandom::fill(&mut bytes).map_err(io::Error::other)?;
            self.receiver_channel = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes);
        }
        Ok(self)
    }

    pub fn with_outbound_channel(mut self, channel: &str, server: &str) -> io::Result<Self> {
        self.outbound_channel = clean_channel(channel)?;
        if self.outbound_channel.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "relay code is required",
            ));
        }
        self.server = normalize_server(server)?;
        Ok(self)
    }

    pub fn endpoint(&self, channel: &str) -> io::Result<String> {
        let channel = clean_channel(channel)?;
        if channel.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "relay code is required",
            ));
        }
        Ok(format!(
            "{}/api/leds/{channel}",
            normalize_server(&self.server)?
        ))
    }
}

pub fn load_config(path: &Path, machine_name: &str) -> io::Result<RelayConfig> {
    let document = match fs::read(path) {
        Ok(bytes) => serde_json::from_slice::<Value>(&bytes).ok(),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(error),
    };
    Ok(document.map_or_else(
        || RelayConfig::default_for_host(machine_name),
        |document| RelayConfig::from_legacy_json(&document, machine_name),
    ))
}

pub fn save_config(path: &Path, config: &RelayConfig) -> io::Result<()> {
    RelayConfigStore::load(path, &config.machine_name)?.save(config.clone())
}

pub fn normalize_server(input: &str) -> io::Result<String> {
    let url = Url::parse(input.trim()).map_err(|_| invalid_server())?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(invalid_server());
    }
    Ok(url.as_str().trim_end_matches('/').to_owned())
}

pub fn clean_channel(channel: &str) -> io::Result<String> {
    let channel = channel.trim();
    if !channel.is_empty() && !(11..=128).contains(&channel.len())
        || !channel
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-')
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "relay code must be 11-128 Base64URL characters",
        ));
    }
    Ok(channel.to_owned())
}

fn clean_machine_name(name: &str) -> String {
    let name = name.trim().chars().take(80).collect::<String>();
    if name.is_empty() {
        "Remote computer".into()
    } else {
        name
    }
}

fn invalid_server() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidInput,
        "bridge server must be an HTTP or HTTPS origin",
    )
}

/// Publish one local provider event to the configured receiver channel.
pub fn publish_event(config: &RelayConfig, provider: &str, line: &Value) -> io::Result<()> {
    if !["codex", "claude", "grok", "cursor", "junie"].contains(&provider) || !line.is_object() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid provider event",
        ));
    }
    let url = config.endpoint(&config.outbound_channel)?;
    let body = json!({
        "v": 1,
        "type": "agent_event",
        "event_id": uuid::Uuid::new_v4().to_string(),
        "source": {"name": config.machine_name},
        "provider": provider,
        "line": line,
    });
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(5))
        .timeout_read(Duration::from_secs(5))
        .timeout_write(Duration::from_secs(5))
        .build();
    agent
        .post(&url)
        .set("Content-Type", "application/json")
        .send_string(&body.to_string())
        .map_err(io::Error::other)?;
    Ok(())
}

/// Read one SSE connection. The caller handles reconnects and event parsing.
pub fn receive_once(
    config: &RelayConfig,
    on_event: impl FnMut(String) -> io::Result<()>,
) -> io::Result<()> {
    receive_once_while(config, || true, on_event)
}

pub fn receive_once_while(
    config: &RelayConfig,
    mut is_current: impl FnMut() -> bool,
    mut on_event: impl FnMut(String) -> io::Result<()>,
) -> io::Result<()> {
    let url = config.endpoint(&config.receiver_channel)?;
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(5))
        .timeout_read(Duration::from_secs(30))
        .build();
    let response = agent
        .get(&url)
        .set("Accept", "text/event-stream")
        .call()
        .map_err(io::Error::other)?;
    let mut reader = BufReader::new(response.into_reader());
    let mut decoder = SseDecoder::default();
    loop {
        let mut line = Vec::new();
        let count = (&mut reader)
            .take(1024 * 1024 + 1)
            .read_until(b'\n', &mut line)?;
        if count > 1024 * 1024 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "SSE line exceeds 1 MiB",
            ));
        }
        if count == 0 {
            if !is_current() {
                return Ok(());
            }
            if let Some(message) = decoder.finish() {
                on_event(message)?;
            }
            return Ok(());
        }
        if !is_current() {
            return Ok(());
        }
        let line = String::from_utf8_lossy(&line);
        if let Some(message) = decoder.push_line(&line)? {
            on_event(message)?;
        }
    }
}

#[derive(Default)]
pub struct SseDecoder {
    lines: Vec<String>,
    size: usize,
}

impl SseDecoder {
    pub fn push_line(&mut self, line: &str) -> io::Result<Option<String>> {
        let line = line.trim_end_matches(['\r', '\n']);
        if line.is_empty() {
            return Ok(self.finish());
        }
        if let Some(value) = line.strip_prefix("data:") {
            let value = value.strip_prefix(' ').unwrap_or(value);
            self.size += value.len();
            if self.size > 1024 * 1024 {
                self.lines.clear();
                self.size = 0;
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "SSE event exceeds 1 MiB",
                ));
            }
            self.lines.push(value.to_owned());
        }
        Ok(None)
    }

    pub fn finish(&mut self) -> Option<String> {
        self.size = 0;
        if self.lines.is_empty() {
            None
        } else {
            Some(std::mem::take(&mut self.lines).join("\n"))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn legacy_config_round_trips_and_keeps_private_permissions() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("relay.json");
        let config = RelayConfig::default_for_host("Laptop")
            .with_receiver_channel()
            .unwrap()
            .with_outbound_channel(&"a".repeat(22), DEFAULT_BRIDGE_SERVER)
            .unwrap();
        assert_eq!(config.receiver_channel.len(), 22);
        assert_eq!(
            config.receiver_channel,
            config
                .clone()
                .with_receiver_channel()
                .unwrap()
                .receiver_channel
        );
        save_config(&path, &config).unwrap();
        assert_eq!(load_config(&path, "Different").unwrap(), config);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn invalid_settings_and_servers_are_rejected_or_fall_back() {
        assert!(normalize_server("https://user@bridge.example.com/path").is_err());
        assert!(normalize_server("ftp://bridge.example.com").is_err());
        assert!(clean_channel("../bad").is_err());
        let config = RelayConfig::from_legacy_json(
            &json!({"version": 2, "server": "https://other.example.com"}),
            "Host",
        );
        assert_eq!(config.server, DEFAULT_BRIDGE_SERVER);
    }

    #[test]
    fn parses_multiline_sse_and_ignores_comments() {
        let mut decoder = SseDecoder::default();
        assert_eq!(decoder.push_line(": keepalive\n").unwrap(), None);
        assert_eq!(decoder.push_line("data: first\r\n").unwrap(), None);
        assert_eq!(decoder.push_line("data: second\n").unwrap(), None);
        assert_eq!(
            decoder.push_line("\n").unwrap(),
            Some("first\nsecond".into())
        );
    }

    #[test]
    fn publishes_legacy_envelope_over_http() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let server = format!("http://{}", listener.local_addr().unwrap());
        let expected = "a".repeat(22);
        let receiver = thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream);
            let mut request_line = String::new();
            reader.read_line(&mut request_line).unwrap();
            let mut body_length = 0;
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" {
                    break;
                }
                if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                    body_length = value.trim().parse::<usize>().unwrap();
                }
            }
            let mut body = vec![0; body_length];
            reader.read_exact(&mut body).unwrap();
            reader
                .get_mut()
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                .unwrap();
            (
                request_line,
                serde_json::from_slice::<Value>(&body).unwrap(),
            )
        });
        let config = RelayConfig::default_for_host("Desktop")
            .with_outbound_channel(&expected, &server)
            .unwrap();
        publish_event(&config, "claude", &json!({"hook_event_name": "Stop"})).unwrap();
        let (request_line, body) = receiver.join().unwrap();
        assert!(request_line.starts_with(&format!("POST /api/leds/{expected} HTTP/1.1")));
        assert_eq!(body["source"]["name"], "Desktop");
        assert_eq!(body["line"]["hook_event_name"], "Stop");
        assert_eq!(body["type"], "agent_event");
    }

    #[test]
    fn receives_multiline_sse_over_http() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let server = format!("http://{}", listener.local_addr().unwrap());
        let receiver = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0; 1024];
            let count = stream.read(&mut request).unwrap();
            assert!(String::from_utf8_lossy(&request[..count]).starts_with("GET /api/leds/"));
            stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n: heartbeat\ndata: first\ndata: second\n\n").unwrap();
        });
        let mut config = RelayConfig::default_for_host("Desktop")
            .with_receiver_channel()
            .unwrap();
        config.server = server;
        let mut received = Vec::new();
        receive_once(&config, |message| {
            received.push(message);
            Ok(())
        })
        .unwrap();
        assert_eq!(received, ["first\nsecond"]);
        receiver.join().unwrap();
    }
}

#[cfg(test)]
mod cancellation_tests {
    use super::*;
    use std::net::TcpListener;
    use std::sync::atomic::{AtomicBool, Ordering};
    #[test]
    fn replacing_configuration_stops_an_existing_event_stream() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let mut config = RelayConfig::default_for_host("Test")
            .with_receiver_channel()
            .unwrap();
        config.server = format!("http://{}", listener.local_addr().unwrap());
        let server = std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream);
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" {
                    break;
                }
            }
            reader.get_mut().write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\ndata: first\n\ndata: stale\n\n").unwrap();
        });
        let current = AtomicBool::new(true);
        let mut seen = Vec::new();
        receive_once_while(
            &config,
            || current.load(Ordering::Acquire),
            |message| {
                seen.push(message);
                current.store(false, Ordering::Release);
                Ok(())
            },
        )
        .unwrap();
        server.join().unwrap();
        assert_eq!(seen, vec!["first"]);
    }
}
