//! Development-only binary for comparing the new core with existing logs.

use std::env;
use std::fs;
use std::process::ExitCode;
use std::time::Duration;

use sidepulse_core::{
    ClientRequest, Monitor, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload,
    parse_log_line,
};

fn main() -> ExitCode {
    let mut args = env::args().skip(1);
    match args.next().as_deref() {
        Some("version") if args.next().is_none() => {
            println!("sidepulse-next {}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        Some("hook-log") => {
            let _ = sidepulse_cli::run_hook(args);
            ExitCode::SUCCESS
        }
        Some("agent-monitor") => match args.next().as_deref() {
            Some("hook-log") => {
                let _ = sidepulse_cli::run_hook(args);
                ExitCode::SUCCESS
            }
            Some("status") => sidepulse_cli::run_status(args),
            Some("watch" | "live") => sidepulse_cli::run_watch(args),
            Some("doctor") => sidepulse_cli::run_doctor(args),
            Some("install") => sidepulse_cli::run_hook_config("install", args),
            Some("uninstall") => sidepulse_cli::run_hook_config("uninstall", args),
            _ => {
                eprintln!(
                    "usage: sidepulse-next agent-monitor <doctor | status | watch | live | hook-log | install | uninstall>"
                );
                ExitCode::from(2)
            }
        },
        Some("status") => sidepulse_cli::run_status(args),
        Some("watch" | "live") => sidepulse_cli::run_watch(args),
        Some("doctor") => sidepulse_cli::run_doctor(args),
        Some("link") => sidepulse_cli::run_link(args),
        Some("battery") => sidepulse_cli::run_battery(args),
        Some("service-status") => {
            let (Some(endpoint), None) = (args.next(), args.next()) else {
                eprintln!("usage: sidepulse-next service-status ENDPOINT");
                return ExitCode::from(2);
            };
            let request = ClientRequest {
                version: PROTOCOL_VERSION,
                request_id: 1,
                kind: RequestKind::Snapshot,
            };
            let reply: ServerMessage =
                match sidepulse_ipc::request(&endpoint, &request, Duration::from_secs(2)) {
                    Ok(reply) => reply,
                    Err(error) => {
                        eprintln!("sidepulse-next: service unavailable: {error}");
                        return ExitCode::FAILURE;
                    }
                };
            match reply.payload {
                ServerPayload::Snapshot { state } => {
                    println!("{}", serde_json::to_string_pretty(&state).unwrap());
                    ExitCode::SUCCESS
                }
                ServerPayload::Error { code, message } => {
                    eprintln!("sidepulse-next: {code}: {message}");
                    ExitCode::FAILURE
                }
                _ => {
                    eprintln!("sidepulse-next: unexpected service reply");
                    ExitCode::FAILURE
                }
            }
        }
        Some("service-settings") => {
            let (Some(endpoint), None) = (args.next(), args.next()) else {
                eprintln!("usage: sidepulse-next service-settings ENDPOINT");
                return ExitCode::from(2);
            };
            service_settings_request(&endpoint, RequestKind::Settings)
        }
        Some("service-power") => {
            let (Some(endpoint), None) = (args.next(), args.next()) else {
                eprintln!("usage: sidepulse-next service-power ENDPOINT");
                return ExitCode::from(2);
            };
            let request = ClientRequest {
                version: PROTOCOL_VERSION,
                request_id: 8,
                kind: RequestKind::Power,
            };
            let reply: ServerMessage =
                match sidepulse_ipc::request(&endpoint, &request, Duration::from_secs(3)) {
                    Ok(reply) => reply,
                    Err(error) => {
                        eprintln!("sidepulse-next: service unavailable: {error}");
                        return ExitCode::FAILURE;
                    }
                };
            match reply.payload {
                ServerPayload::Power { snapshot } => {
                    println!(
                        "{}",
                        serde_json::to_string_pretty(&snapshot).expect("power snapshot serializes")
                    );
                    ExitCode::SUCCESS
                }
                ServerPayload::Error { code, message } => {
                    eprintln!("sidepulse-next: {code}: {message}");
                    ExitCode::FAILURE
                }
                _ => {
                    eprintln!("sidepulse-next: unexpected service reply");
                    ExitCode::FAILURE
                }
            }
        }
        Some("service-devices") => {
            let (Some(endpoint), None) = (args.next(), args.next()) else {
                eprintln!("usage: sidepulse-next service-devices ENDPOINT");
                return ExitCode::from(2);
            };
            service_devices_request(&endpoint, RequestKind::Devices)
        }
        Some("service-select") => {
            let (Some(endpoint), Some(root), None) = (args.next(), args.next(), args.next()) else {
                eprintln!("usage: sidepulse-next service-select ENDPOINT DEVICE_ROOT");
                return ExitCode::from(2);
            };
            service_devices_request(&endpoint, RequestKind::SelectDevice { root })
        }
        Some("service-brightness") => {
            let (Some(endpoint), Some(value), None) = (args.next(), args.next(), args.next())
            else {
                eprintln!("usage: sidepulse-next service-brightness ENDPOINT 0-255");
                return ExitCode::from(2);
            };
            let Ok(brightness) = value.parse::<u8>() else {
                eprintln!("sidepulse-next: brightness must be 0-255");
                return ExitCode::from(2);
            };
            service_settings_request(&endpoint, RequestKind::SetBrightness { brightness })
        }
        Some("service-display") => {
            let (Some(endpoint), Some(mode), None) = (args.next(), args.next(), args.next()) else {
                eprintln!("usage: sidepulse-next service-display ENDPOINT agent|battery");
                return ExitCode::from(2);
            };
            if !matches!(mode.as_str(), "agent" | "battery") {
                eprintln!("sidepulse-next: display mode must be agent or battery");
                return ExitCode::from(2);
            }
            service_settings_request(&endpoint, RequestKind::SetDisplayMode { mode })
        }
        Some("service-sleep-policy") => {
            let (Some(endpoint), Some(policy), None) = (args.next(), args.next(), args.next())
            else {
                eprintln!(
                    "usage: sidepulse-next service-sleep-policy ENDPOINT never|agents|always"
                );
                return ExitCode::from(2);
            };
            service_settings_request(&endpoint, RequestKind::SetSleepPolicy { policy })
        }
        Some("service-transcript") => {
            let (Some(endpoint), Some(provider), Some(state), None) =
                (args.next(), args.next(), args.next(), args.next())
            else {
                eprintln!("usage: sidepulse-next service-transcript ENDPOINT codex|claude on|off");
                return ExitCode::from(2);
            };
            if !matches!(provider.as_str(), "codex" | "claude")
                || !matches!(state.as_str(), "on" | "off")
            {
                eprintln!("usage: sidepulse-next service-transcript ENDPOINT codex|claude on|off");
                return ExitCode::from(2);
            }
            service_settings_request(
                &endpoint,
                RequestKind::SetTranscriptMonitoring {
                    provider,
                    enabled: state == "on",
                },
            )
        }
        Some("inspect-log") => {
            let (Some(provider), Some(path), at, None) =
                (args.next(), args.next(), args.next(), args.next())
            else {
                eprintln!("usage: sidepulse-next inspect-log PROVIDER JSONL_PATH [ISO_TIMESTAMP]");
                return ExitCode::from(2);
            };
            let now = if let Some(value) = at {
                match chrono::DateTime::parse_from_rfc3339(&value) {
                    Ok(value) => value.with_timezone(&chrono::Utc),
                    Err(error) => {
                        eprintln!("sidepulse-next: invalid timestamp: {error}");
                        return ExitCode::from(2);
                    }
                }
            } else {
                chrono::Utc::now()
            };
            let text = match fs::read_to_string(&path) {
                Ok(text) => text,
                Err(error) => {
                    eprintln!("sidepulse-next: {path}: {error}");
                    return ExitCode::FAILURE;
                }
            };
            let mut monitor = Monitor::default();
            for line in text.lines() {
                if let Some(event) = parse_log_line(&provider, line) {
                    monitor.ingest(&event);
                }
            }
            match serde_json::to_string_pretty(&monitor.snapshot(now)) {
                Ok(output) => {
                    println!("{output}");
                    ExitCode::SUCCESS
                }
                Err(error) => {
                    eprintln!("sidepulse-next: {error}");
                    ExitCode::FAILURE
                }
            }
        }
        _ => {
            eprintln!(
                "usage: sidepulse-next <version | doctor [--json] | status [--json] | battery status [--json] [--full-watts auto|WATTS] | link [RELAY_CODE] [--server ORIGIN] [--config PATH] | hook-log --provider PROVIDER --log PATH | agent-monitor <doctor | status | hook-log | install | uninstall> | service-status ENDPOINT | service-settings ENDPOINT | service-power ENDPOINT | service-devices ENDPOINT | service-select ENDPOINT DEVICE_ROOT | service-brightness ENDPOINT 0-255 | service-display ENDPOINT agent|battery | service-sleep-policy ENDPOINT never|agents|always | inspect-log PROVIDER JSONL_PATH [ISO_TIMESTAMP]>"
            );
            ExitCode::from(2)
        }
    }
}

fn service_settings_request(endpoint: &str, kind: RequestKind) -> ExitCode {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind,
    };
    let reply: ServerMessage =
        match sidepulse_ipc::request(endpoint, &request, Duration::from_secs(2)) {
            Ok(reply) => reply,
            Err(error) => {
                eprintln!("sidepulse-next: service unavailable: {error}");
                return ExitCode::FAILURE;
            }
        };
    if reply.version != PROTOCOL_VERSION || reply.request_id != Some(1) {
        eprintln!("sidepulse-next: invalid service response");
        return ExitCode::FAILURE;
    }
    match reply.payload {
        ServerPayload::Settings { settings, .. } => {
            println!(
                "{}",
                serde_json::to_string_pretty(&settings).expect("settings JSON serializes")
            );
            ExitCode::SUCCESS
        }
        ServerPayload::Error { code, message } => {
            eprintln!("sidepulse-next: {code}: {message}");
            ExitCode::FAILURE
        }
        _ => {
            eprintln!("sidepulse-next: unexpected service reply");
            ExitCode::FAILURE
        }
    }
}

fn service_devices_request(endpoint: &str, kind: RequestKind) -> ExitCode {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 6,
        kind,
    };
    let reply = match sidepulse_ipc::request::<_, ServerMessage>(
        endpoint,
        &request,
        Duration::from_secs(2),
    ) {
        Ok(reply) => reply,
        Err(error) => {
            eprintln!("sidepulse-next: service unavailable: {error}");
            return ExitCode::FAILURE;
        }
    };
    match reply.payload {
        ServerPayload::Devices {
            devices,
            active_device,
        } => {
            println!(
                "{}",
                serde_json::to_string_pretty(
                    &serde_json::json!({"devices": devices, "active_device": active_device})
                )
                .unwrap()
            );
            ExitCode::SUCCESS
        }
        ServerPayload::Error { code, message } => {
            eprintln!("sidepulse-next: {code}: {message}");
            ExitCode::FAILURE
        }
        _ => {
            eprintln!("sidepulse-next: unexpected service reply");
            ExitCode::FAILURE
        }
    }
}
