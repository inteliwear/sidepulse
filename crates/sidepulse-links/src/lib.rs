//! Phone credentials, pairing and notification transport. No GUI dependency.

use base64::Engine;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sidepulse_core::PhoneLinkSummary;
use sidepulse_relay::{DEFAULT_BRIDGE_SERVER, SseDecoder, normalize_server};
use std::{
    fs,
    io::{self, BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

pub fn normalize_token(value: &str) -> io::Result<String> {
    let token = value
        .chars()
        .filter(|character| !character.is_whitespace())
        .collect::<String>()
        .to_ascii_lowercase();
    if token.len() != 64 || !token.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(invalid(
            "push token must be exactly 64 hexadecimal characters",
        ));
    }
    Ok(token)
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PhoneLink {
    pub name: String,
    pub token: String,
    pub server: String,
    pub linked_at: String,
}
impl PhoneLink {
    pub fn new(name: &str, token: &str, server: &str) -> io::Result<Self> {
        let name = name.trim().chars().take(80).collect::<String>();
        Ok(Self {
            name: if name.is_empty() {
                "iPhone".into()
            } else {
                name
            },
            token: normalize_token(token)?,
            server: normalize_server(server)?,
            linked_at: chrono::Utc::now().to_rfc3339(),
        })
    }
    pub fn summary(&self) -> PhoneLinkSummary {
        PhoneLinkSummary {
            id: self.token[..12].into(),
            name: self.name.clone(),
            server: self.server.clone(),
            linked_at: self.linked_at.clone(),
            display: "agent".into(),
            last_sent_at: None,
            delivery_error: None,
        }
    }
    fn document(&self) -> Value {
        let mut value = serde_json::to_value(self).unwrap();
        value["id"] = self.summary().id.into();
        value
    }
}
fn link_from_value(value: &Value) -> Option<PhoneLink> {
    let mut link = PhoneLink::new(
        value
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("iPhone"),
        value.get("token")?.as_str()?,
        value
            .get("server")
            .and_then(Value::as_str)
            .unwrap_or(DEFAULT_BRIDGE_SERVER),
    )
    .ok()?;
    link.linked_at = value
        .get("linked_at")
        .and_then(Value::as_str)
        .unwrap_or("")
        .into();
    Some(link)
}

fn read_links_file(path: &Path) -> io::Result<Vec<u8>> {
    let mut bytes = Vec::new();
    fs::File::open(path)?
        .take(1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > 1024 * 1024 {
        return Err(invalid("phone links file is too large"));
    }
    Ok(bytes)
}

pub struct PhoneStore {
    path: PathBuf,
    original: Option<Vec<u8>>,
    document: serde_json::Map<String, Value>,
}
impl PhoneStore {
    pub fn load(path: &Path) -> io::Result<Self> {
        let original = match read_links_file(path) {
            Ok(bytes) => Some(bytes),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        let document = if let Some(bytes) = &original {
            serde_json::from_slice::<Value>(bytes)?
                .as_object()
                .cloned()
                .ok_or_else(|| invalid("phone links must be an object"))?
        } else {
            serde_json::Map::new()
        };
        Ok(Self {
            path: path.into(),
            original,
            document,
        })
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    pub fn links(&self) -> Vec<PhoneLink> {
        if self.document.get("version").and_then(Value::as_u64) != Some(1) {
            return Vec::new();
        }
        self.document
            .get("ios")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(link_from_value)
            .collect()
    }
    pub fn register(&mut self, link: PhoneLink) -> io::Result<()> {
        let mut updated = self.document.clone();
        updated.insert("version".into(), json!(1));
        let list = updated
            .entry("ios")
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .ok_or_else(|| invalid("ios links must be an array"))?;
        if let Some(existing) = list.iter_mut().find(|value| {
            link_from_value(value).is_some_and(|existing| existing.token == link.token)
        }) {
            let item = existing.as_object_mut().unwrap();
            item.extend(link.document().as_object().unwrap().clone());
        } else {
            list.push(link.document());
        }
        self.save(updated)
    }
    pub fn remove(&mut self, id: &str) -> io::Result<()> {
        let matches: Vec<_> = self
            .links()
            .into_iter()
            .filter(|link| link.summary().id == id)
            .collect();
        if matches.len() != 1 {
            return Err(invalid("phone identifier is missing or ambiguous"));
        }
        let token = &matches[0].token;
        let mut updated = self.document.clone();
        if let Some(items) = updated.get_mut("ios").and_then(Value::as_array_mut) {
            items.retain(|value| link_from_value(value).is_none_or(|link| link.token != *token));
        }
        self.save(updated)
    }
    fn save(&mut self, updated: serde_json::Map<String, Value>) -> io::Result<()> {
        if fs::symlink_metadata(&self.path).is_ok_and(|metadata| metadata.file_type().is_symlink())
        {
            return Err(invalid("refusing to replace a phone links symlink"));
        }
        let current = match read_links_file(&self.path) {
            Ok(bytes) => Some(bytes),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        if current != self.original {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "phone links changed externally; reload before saving",
            ));
        }
        let parent = self.path.parent().unwrap_or(Path::new("."));
        fs::create_dir_all(parent)?;
        let mut file = tempfile::NamedTempFile::new_in(parent)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.as_file()
                .set_permissions(fs::Permissions::from_mode(0o600))?;
        }
        let mut bytes = serde_json::to_vec_pretty(&updated)?;
        bytes.push(b'\n');
        file.write_all(&bytes)?;
        file.as_file().sync_all()?;
        file.persist(&self.path).map_err(|error| error.error)?;
        self.original = Some(bytes);
        self.document = updated;
        Ok(())
    }
}

pub fn new_pairing_channel() -> io::Result<String> {
    let mut bytes = [0u8; 8];
    getrandom::fill(&mut bytes).map_err(io::Error::other)?;
    Ok(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes))
}
pub fn pairing_url(server: &str, channel: &str, sender: &str) -> io::Result<String> {
    let server = normalize_server(server)?;
    if channel.len() != 11
        || !channel
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
    {
        return Err(invalid("invalid phone pairing channel"));
    }
    if server == DEFAULT_BRIDGE_SERVER {
        return Ok(format!("sidepulse://p/{channel}"));
    }
    let mut query = url::form_urlencoded::Serializer::new(String::new());
    query
        .append_pair("v", "1")
        .append_pair("server", &server)
        .append_pair("channel", channel);
    let sender: String = sender.trim().chars().take(80).collect();
    if !sender.is_empty() {
        query.append_pair("sender", &sender);
    }
    Ok(format!("sidepulse://pair?{}", query.finish()))
}
pub fn qr_matrix(url: &str) -> io::Result<Vec<Vec<bool>>> {
    let code = qrcode::QrCode::new(url.as_bytes()).map_err(io::Error::other)?;
    let width = code.width();
    let mut matrix = vec![vec![false; width + 4]; width + 4];
    for y in 0..width {
        for x in 0..width {
            matrix[y + 2][x + 2] = code[(x, y)] == qrcode::Color::Dark;
        }
    }
    Ok(matrix)
}
pub fn parse_registration(text: &str, server: &str) -> io::Result<PhoneLink> {
    let document: Value = serde_json::from_str(text)?;
    if document.get("v").and_then(Value::as_u64) != Some(1)
        || document.get("type").and_then(Value::as_str) != Some("ios_registration")
    {
        return Err(invalid("unsupported phone registration"));
    }
    let device = document
        .get("device")
        .ok_or_else(|| invalid("phone registration has no device"))?;
    if device.get("bundle_id").and_then(Value::as_str) != Some("io.sidepulse.ios") {
        return Err(invalid("unsupported phone application"));
    }
    PhoneLink::new(
        device
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("iPhone"),
        device
            .get("push_token")
            .and_then(Value::as_str)
            .unwrap_or(""),
        server,
    )
}
pub fn receive_registration_once(
    server: &str,
    channel: &str,
    timeout: Duration,
    mut current: impl FnMut() -> bool,
) -> io::Result<Option<PhoneLink>> {
    if !current() {
        return Ok(None);
    }
    pairing_url(server, channel, "")?;
    let url = format!("{}/api/leds/{channel}", normalize_server(server)?);
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(timeout.min(Duration::from_secs(5)))
        .timeout_read(timeout.min(Duration::from_secs(20)))
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
            return Err(invalid("pairing event is too large"));
        }
        if !current() {
            return Ok(None);
        }
        let message = if count == 0 {
            decoder.finish()
        } else {
            decoder.push_line(&String::from_utf8_lossy(&line))?
        };
        if let Some(message) = message
            && let Ok(link) = parse_registration(&message, server)
        {
            return Ok(Some(link));
        }
        if count == 0 {
            return Ok(None);
        }
    }
}

pub fn notification_payload(
    program: Option<&str>,
    title: Option<&str>,
    message: Option<&str>,
    event_id: &str,
    data: &Value,
) -> io::Result<Value> {
    let title = title.map(str::trim).filter(|title| !title.is_empty());
    let message = message.map(str::trim).filter(|message| !message.is_empty());
    if program.is_none() && title.is_none() && message.is_none() {
        return Err(invalid("remote delivery requires LEDs, title, or message"));
    }
    let mut data = data.as_object().cloned().unwrap_or_default();
    data.insert("sidepulse_event_id".into(), event_id.into());
    let mut aps = json!({"content-available":1});
    if title.is_some() || message.is_some() {
        let mut alert = serde_json::Map::new();
        if let Some(title) = title {
            alert.insert("title".into(), title.into());
        }
        if let Some(message) = message {
            alert.insert("body".into(), message.into());
        }
        aps["alert"] = Value::Object(alert);
    }
    let mut payload = json!({"aps":aps,"data":data});
    if let Some(program) = program {
        payload["leds"] = program.into();
    }
    if let Some(title) = title {
        payload["title"] = title.into();
    }
    if let Some(message) = message {
        payload["body"] = message.into();
    }
    Ok(payload)
}
pub fn send_program(
    link: &PhoneLink,
    program: Option<&str>,
    title: Option<&str>,
    message: Option<&str>,
    event_id: &str,
    data: &Value,
) -> io::Result<String> {
    let token = normalize_token(&link.token)?;
    let server = normalize_server(&link.server)?;
    let payload = notification_payload(program, title, message, event_id, data)?;
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(5))
        .timeout_read(Duration::from_secs(10))
        .timeout_write(Duration::from_secs(10))
        .build();
    let response = agent
        .post(&format!("{server}/api/leds/apns_{token}"))
        .set("Content-Type", "application/json")
        .send_string(&payload.to_string())
        .map_err(|error| io::Error::other(error.to_string().replace(&token, "[phone]")))?;
    let mut text = String::new();
    response
        .into_reader()
        .take(65537)
        .read_to_string(&mut text)?;
    if text.len() > 65536 {
        return Err(invalid("bridge response is too large"));
    }
    Ok(text.trim().into())
}
