//! Development hook entry point. It is deliberately independent of the UI.

use std::collections::HashMap;
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
#[cfg(unix)]
use std::process::Command;
use std::time::Duration;

use chrono::Utc;
use serde_json::{Value, json};
use sidepulse_core::{
    AgentOrigin, ClientRequest, Monitor, PROTOCOL_VERSION, ProcessInfo, RequestKind, ServerMessage,
    format_hook_payload, infer_hook_provider, normalize_cursor_payload, origin_from_environment,
    origin_from_processes, origin_from_terminal_environment, parse_log_line, status_audit_record,
};

const MAX_STDIN_BYTES: u64 = 8 * 1024 * 1024;

struct Options {
    provider: String,
    log: PathBuf,
    audit: PathBuf,
    event: Option<String>,
    endpoint: Option<String>,
}

/// Hooks execute inside another program's turn. Callers deliberately ignore
/// ordinary failures and keep stdout reserved for the provider protocol.
pub fn run_hook(args: impl Iterator<Item = String>) -> io::Result<()> {
    let Some(options) = parse_args(args) else {
        return Ok(());
    };
    let mut payload = String::new();
    io::stdin()
        .take(MAX_STDIN_BYTES + 1)
        .read_to_string(&mut payload)?;
    if payload.len() as u64 > MAX_STDIN_BYTES {
        return Ok(());
    }
    let provider = options.provider.as_str();
    if provider == "cursor" {
        if let Some(event) = options.event.as_deref() {
            let raw: Value = serde_json::from_str(&payload).unwrap_or(Value::Null);
            payload = normalize_cursor_payload(event, &raw).to_string();
        }
    } else if provider == "junie" {
        let raw: Value = serde_json::from_str(&payload).unwrap_or(Value::Null);
        payload = normalize_junie_payload(&raw, &options.log, junie_process_id()).to_string();
    }
    let mut line = format_hook_payload(provider, &payload, Utc::now());
    let actual_provider = infer_hook_provider(provider, &line);
    annotate_origin(&actual_provider, &mut line);
    let log = if actual_provider != provider {
        options
            .log
            .with_file_name(format!("{actual_provider}.jsonl"))
    } else {
        options.log
    };
    let _ = append_line(&log, &line);
    if let Some(event) = parse_log_line(&actual_provider, &line.to_string()) {
        let mut monitor = Monitor::default();
        let status = monitor.ingest(&event);
        let audit = status_audit_record(&event, status, Utc::now());
        let _ = append_line(&options.audit, &audit);
    }
    if !socket_disabled()
        && let Some(endpoint) = options
            .endpoint
            .or_else(|| env::var("SIDEPULSE_NEXT_ENDPOINT").ok())
    {
        let request = ClientRequest {
            version: PROTOCOL_VERSION,
            request_id: 1,
            kind: RequestKind::IngestHook {
                provider: actual_provider,
                line,
            },
        };
        let _: io::Result<ServerMessage> =
            sidepulse_ipc::request(&endpoint, &request, Duration::from_millis(200));
    }
    Ok(())
}

fn parse_args(mut args: impl Iterator<Item = String>) -> Option<Options> {
    let (mut provider, mut log, mut event, mut endpoint, mut audit) =
        (None, None, None, None, None);
    while let Some(arg) = args.next() {
        let value = args.next()?;
        match arg.as_str() {
            "--provider" => provider = Some(value),
            "--log" => log = Some(PathBuf::from(value)),
            "--audit" => audit = Some(PathBuf::from(value)),
            "--event" => event = Some(value),
            "--endpoint" => endpoint = Some(value),
            _ => return None,
        }
    }
    let log = expand_home(&log?);
    let audit = audit
        .map(|path| expand_home(&path))
        .unwrap_or_else(|| log.with_file_name("event-status.jsonl"));
    Some(Options {
        provider: provider?,
        log,
        audit,
        event,
        endpoint,
    })
}

fn expand_home(path: &Path) -> PathBuf {
    if let Ok(rest) = path.strip_prefix("~")
        && let Some(home) = env::var_os("HOME")
    {
        return PathBuf::from(home).join(rest);
    }
    path.to_path_buf()
}

fn append_line(path: &Path, line: &Value) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut bytes = serde_json::to_vec(line).map_err(io::Error::other)?;
    bytes.push(b'\n');
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?
        .write_all(&bytes)
}

fn socket_disabled() -> bool {
    env::var("SIDEPULSE_DISABLE_EVENT_SOCKET")
        .is_ok_and(|value| matches!(value.to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
}

fn annotate_origin(provider: &str, line: &mut Value) {
    let raw = if provider == "codex" {
        line.get_mut("event")
    } else {
        Some(line)
    };
    let Some(raw) = raw.and_then(Value::as_object_mut) else {
        return;
    };
    if raw.contains_key("agent_origin") || raw.contains_key("agentOrigin") {
        return;
    }
    let environment = env::vars_os()
        .filter_map(|(key, value)| Some((key.into_string().ok()?, value.into_string().ok()?)))
        .collect::<HashMap<_, _>>();
    let origin = origin_from_environment(provider, &environment)
        .or_else(|| origin_from_processes(provider, &process_ancestry()))
        .or_else(|| origin_from_terminal_environment(provider, &environment))
        .unwrap_or_else(|| AgentOrigin::fallback(provider));
    for (key, value) in [
        ("agent_origin", origin.label),
        ("agent_origin_kind", origin.kind),
        ("agent_origin_source", origin.source),
        ("agent_origin_confidence", origin.confidence),
    ] {
        raw.insert(key.into(), json!(value));
    }
}

fn normalize_junie_payload(raw: &Value, log: &Path, process_id: Option<u32>) -> Value {
    let mut result = raw.as_object().cloned().unwrap_or_default();
    if let Some(process_id) = process_id {
        result
            .entry("sidepulse_junie_process_id".to_owned())
            .or_insert_with(|| json!(process_id));
    }
    if result
        .get("session_id")
        .is_some_and(|value| !value.is_null())
        && result.get("cwd").is_some_and(|value| !value.is_null())
    {
        return Value::Object(result);
    }
    // Junie omits context on terminal events. Recover it from recent JSONL.
    if let Ok(mut file) = fs::File::open(log) {
        let Ok(length) = file.metadata().map(|metadata| metadata.len()) else {
            return Value::Object(result);
        };
        let start = length.saturating_sub(1024 * 1024);
        if file.seek(SeekFrom::Start(start)).is_err() {
            return Value::Object(result);
        }
        let mut bytes = Vec::new();
        if file.read_to_end(&mut bytes).is_err() {
            return Value::Object(result);
        }
        let text = String::from_utf8_lossy(&bytes);
        let mut fallback = None;
        let mut matching = None;
        for row in text.lines().rev().take(200) {
            let Ok(Value::Object(previous)) = serde_json::from_str(row) else {
                continue;
            };
            if previous.get("session_id").is_none_or(Value::is_null) {
                continue;
            }
            if fallback.is_none() {
                fallback = Some(previous.clone());
            }
            if process_id
                .is_some_and(|id| previous.get("sidepulse_junie_process_id") == Some(&json!(id)))
            {
                matching = Some(previous);
                break;
            }
        }
        if let Some(previous) = matching.or(fallback) {
            for key in ["session_id", "cwd", "project_path"] {
                if let Some(value) = previous.get(key) {
                    result.entry(key).or_insert_with(|| value.clone());
                }
            }
        }
    }
    Value::Object(result)
}

#[cfg(any(unix, windows))]
fn junie_process_id() -> Option<u32> {
    process_ancestry().into_iter().find_map(|info| {
        let comm = info.comm.to_ascii_lowercase();
        let command = info.command.to_ascii_lowercase();
        let basename = Path::new(&comm)
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or(&comm);
        (basename.trim_end_matches(".exe") == "junie"
            || [
                "/junie.app/",
                "junie-release-",
                "matterhorn.ej.app.cli.standalone",
            ]
            .iter()
            .any(|marker| command.contains(marker)))
        .then_some(info.pid)
    })
}

#[cfg(unix)]
fn process_ancestry() -> Vec<ProcessInfo> {
    let mut current = std::process::id();
    let mut processes = Vec::new();
    for _ in 0..10 {
        let output = Command::new("/bin/ps")
            .args([
                "-p",
                &current.to_string(),
                "-o",
                "pid=",
                "-o",
                "ppid=",
                "-o",
                "comm=",
                "-o",
                "command=",
            ])
            .output();
        let Ok(output) = output else { break };
        if !output.status.success() {
            break;
        }
        let text = String::from_utf8_lossy(&output.stdout);
        let mut parts = text.split_whitespace();
        let (Some(pid), Some(parent), Some(comm)) = (
            parts.next().and_then(|part| part.parse::<u32>().ok()),
            parts.next().and_then(|part| part.parse::<u32>().ok()),
            parts.next(),
        ) else {
            break;
        };
        let command = parts.collect::<Vec<_>>().join(" ");
        processes.push(ProcessInfo {
            pid,
            ppid: Some(parent),
            comm: comm.to_owned(),
            command,
        });
        if parent <= 1 || parent == current {
            break;
        }
        current = parent;
    }
    processes
}

#[cfg(not(any(unix, windows)))]
fn junie_process_id() -> Option<u32> {
    None
}

#[cfg(windows)]
fn process_ancestry() -> Vec<ProcessInfo> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, PROCESSENTRY32W, Process32FirstW, Process32NextW,
        TH32CS_SNAPPROCESS,
    };

    // One snapshot is enough to correlate all parents without starting a
    // shell or waiting on another process in the time-sensitive hook path.
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Vec::new();
    }
    let mut entries = HashMap::new();
    let mut entry = PROCESSENTRY32W {
        dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32,
        ..Default::default()
    };
    if unsafe { Process32FirstW(snapshot, &mut entry) } != 0 {
        loop {
            let end = entry
                .szExeFile
                .iter()
                .position(|character| *character == 0)
                .unwrap_or(entry.szExeFile.len());
            let comm = String::from_utf16_lossy(&entry.szExeFile[..end]);
            entries.insert(
                entry.th32ProcessID,
                ProcessInfo {
                    pid: entry.th32ProcessID,
                    ppid: Some(entry.th32ParentProcessID),
                    command: comm.clone(),
                    comm,
                },
            );
            if unsafe { Process32NextW(snapshot, &mut entry) } == 0 {
                break;
            }
        }
    }
    unsafe { CloseHandle(snapshot) };
    let mut processes = Vec::new();
    let mut current = std::process::id();
    for _ in 0..10 {
        let Some(info) = entries.get(&current) else {
            break;
        };
        processes.push(info.clone());
        let Some(parent) = info.ppid else { break };
        if parent <= 1 || parent == current {
            break;
        }
        current = parent;
    }
    processes
}

#[cfg(not(any(unix, windows)))]
fn process_ancestry() -> Vec<ProcessInfo> {
    Vec::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(any(unix, windows))]
    #[test]
    fn reads_process_ancestry_for_the_hook_process() {
        let processes = process_ancestry();
        assert_eq!(
            processes.first().map(|info| info.pid),
            Some(std::process::id())
        );
    }

    #[test]
    fn junie_terminal_event_prefers_matching_process_context() {
        let path = std::env::temp_dir().join(format!(
            "sidepulse-junie-context-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::write(
            &path,
            concat!(
                "{\"session_id\":\"old\",\"cwd\":\"/old\",\"sidepulse_junie_process_id\":11}\n",
                "{\"session_id\":\"new\",\"cwd\":\"/new\",\"sidepulse_junie_process_id\":22}\n"
            ),
        )
        .unwrap();
        let result = normalize_junie_payload(&json!({"hook_event_name":"Stop"}), &path, Some(11));
        assert_eq!(result["session_id"], "old");
        assert_eq!(result["cwd"], "/old");
        assert_eq!(result["sidepulse_junie_process_id"], 11);
        let fallback = normalize_junie_payload(&json!({"hook_event_name":"Stop"}), &path, None);
        assert_eq!(fallback["session_id"], "new");
        fs::remove_file(path).unwrap();
    }
}
