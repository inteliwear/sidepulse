//! Development-only binary for comparing the new core with existing logs.

use std::env;
use std::fs;
use std::io::Read;
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
            Some("leds") => sidepulse_cli::run_leds("agent", args),
            Some("status-bar") => sidepulse_cli::run_status_bar(args),
            Some("version") if args.next().is_none() => {
                println!("sidepulse-next {}", env!("CARGO_PKG_VERSION"));
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
        Some(command @ ("write" | "push")) => sidepulse_cli::run_delivery(command, args),
        Some("phone-link") => sidepulse_cli::run_phone_link(args),
        Some("reply") => sidepulse_cli::run_reply(args),
        Some("setup") => sidepulse_cli::run_setup(args),
        Some("service") => {
            sidepulse_cli::run_lifecycle(sidepulse_installer::startup::Job::Service, args)
        }
        Some("link") => sidepulse_cli::run_link(args),
        Some("sdejectguard") => sidepulse_cli::run_sd_guard(args),
        Some("status-bar") => sidepulse_cli::run_status_bar(args),
        Some("battery") => sidepulse_cli::run_battery(args),
        Some(command @ ("settings" | "virtual-display")) => {
            let endpoint = match (args.next(), args.next(), args.next()) {
                (Some(flag), Some(endpoint), None) if flag == "--endpoint" => Some(endpoint),
                (None, None, None) => env::var("SIDEPULSE_NEXT_ENDPOINT").ok(),
                _ => None,
            };
            let Some(endpoint) = endpoint else {
                eprintln!(
                    "usage: sidepulse-next {command} --endpoint ENDPOINT (or SIDEPULSE_NEXT_ENDPOINT)"
                );
                return ExitCode::from(2);
            };
            let (bundle, binary) = if command == "settings" {
                ("SidePulse Settings", "sidepulse-next-settings")
            } else {
                ("SidePulse Virtual", "sidepulse-next-virtual")
            };
            let result = env::current_exe().and_then(|path| {
                let executable = if cfg!(target_os = "macos") {
                    path.ancestors()
                        .take(3)
                        .map(|root| {
                            root.join(format!("applications/{bundle}.app/Contents/MacOS/{binary}"))
                        })
                        .find(|candidate| candidate.is_file())
                } else {
                    None
                }
                .unwrap_or_else(|| {
                    path.with_file_name(if cfg!(windows) {
                        format!("{binary}.exe")
                    } else {
                        binary.into()
                    })
                });
                std::process::Command::new(executable).arg(endpoint).spawn()
            });
            match result {
                Ok(_) => ExitCode::SUCCESS,
                Err(error) => {
                    eprintln!("sidepulse-next {command}: {error}");
                    ExitCode::FAILURE
                }
            }
        }
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
        Some(
            command @ ("service-history"
            | "service-virtual-frame"
            | "service-relay-settings"
            | "service-relay-reload"
            | "service-power-control"
            | "service-power-retry"),
        ) => {
            let (Some(endpoint), None) = (args.next(), args.next()) else {
                eprintln!("usage: sidepulse-next {command} ENDPOINT");
                return ExitCode::from(2);
            };
            let kind = match command {
                "service-history" => RequestKind::History,
                "service-relay-settings" => RequestKind::RelaySettings,
                "service-relay-reload" => RequestKind::ReloadRelaySettings,
                "service-power-control" => RequestKind::PowerControl,
                "service-power-retry" => RequestKind::RetryPowerControl,
                _ => RequestKind::VirtualDisplay,
            };
            let request = ClientRequest {
                version: PROTOCOL_VERSION,
                request_id: 1,
                kind,
            };
            let reply: Result<ServerMessage, _> =
                sidepulse_ipc::request(&endpoint, &request, Duration::from_secs(2));
            match reply {
                Ok(ServerMessage {
                    payload: ServerPayload::Error { message, .. },
                    ..
                }) => {
                    eprintln!("sidepulse-next: {message}");
                    ExitCode::FAILURE
                }
                Ok(reply) => {
                    println!(
                        "{}",
                        serde_json::to_string_pretty(&reply.payload)
                            .expect("service payload serializes")
                    );
                    ExitCode::SUCCESS
                }
                Err(error) => {
                    eprintln!("sidepulse-next: service unavailable: {error}");
                    ExitCode::FAILURE
                }
            }
        }
        Some("service-history-timeframe") => {
            let (Some(endpoint), Some(hours), None) = (args.next(), args.next(), args.next())
            else {
                eprintln!("usage: sidepulse-next service-history-timeframe ENDPOINT 1|6|12|24|48");
                return ExitCode::from(2);
            };
            let Some(seconds) = hours
                .parse::<u32>()
                .ok()
                .and_then(|hours| hours.checked_mul(3600))
                .filter(|seconds| sidepulse_core::HISTORY_TIMEFRAMES.contains(seconds))
            else {
                eprintln!("sidepulse-next: history timeframe must be 1, 6, 12, 24, or 48 hours");
                return ExitCode::from(2);
            };
            service_settings_request(&endpoint, RequestKind::SetHistoryTimeframe { seconds })
        }
        Some("service-settings") => {
            let (Some(endpoint), None) = (args.next(), args.next()) else {
                eprintln!("usage: sidepulse-next service-settings ENDPOINT");
                return ExitCode::from(2);
            };
            service_settings_request(&endpoint, RequestKind::Settings)
        }
        Some("open-session") => run_open_session(args),
        Some(command @ ("animation-profile" | "animation-asset")) => {
            run_animation_library(command, args)
        }
        Some("service-session-preference") => {
            let (Some(endpoint), Some(provider), Some(action)) =
                (args.next(), args.next(), args.next())
            else {
                eprintln!(
                    "usage: sidepulse-next service-session-preference ENDPOINT PROVIDER app|terminal|vscode [ORIGIN]"
                );
                return ExitCode::from(2);
            };
            let Some(action) = session_action(&action) else {
                eprintln!("sidepulse-next: action must be app, terminal, or vscode");
                return ExitCode::from(2);
            };
            let origin = args.next();
            if args.next().is_some() {
                eprintln!("sidepulse-next: too many arguments");
                return ExitCode::from(2);
            }
            service_settings_request(
                &endpoint,
                RequestKind::SetSessionOpenPreference {
                    provider,
                    origin,
                    action,
                },
            )
        }
        Some("service-session-terminal") => {
            let (Some(endpoint), Some(terminal)) = (args.next(), args.next()) else {
                eprintln!(
                    "usage: sidepulse-next service-session-terminal ENDPOINT TERMINAL [CUSTOM_PATH]"
                );
                return ExitCode::from(2);
            };
            let custom_path = args.next();
            if args.next().is_some() {
                eprintln!("sidepulse-next: too many arguments");
                return ExitCode::from(2);
            }
            service_settings_request(
                &endpoint,
                RequestKind::SetSessionTerminal {
                    terminal,
                    custom_path,
                },
            )
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
                eprintln!("usage: sidepulse-next service-display ENDPOINT agent|battery|custom");
                return ExitCode::from(2);
            };
            if !matches!(mode.as_str(), "agent" | "battery" | "custom") {
                eprintln!("sidepulse-next: display mode must be agent, battery, or custom");
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
        Some("service-sleep-safeguard") => {
            let (Some(endpoint), Some(value), None) = (args.next(), args.next(), args.next())
            else {
                eprintln!("usage: sidepulse-next service-sleep-safeguard ENDPOINT 0-100");
                return ExitCode::from(2);
            };
            let Ok(percent) = value.parse::<f64>() else {
                return ExitCode::from(2);
            };
            let patch = sidepulse_core::SleepSettingsPatch {
                min_battery_percent: Some(percent),
                ..Default::default()
            };
            if let Err(error) = patch.validate() {
                eprintln!("sidepulse-next: {error}");
                return ExitCode::from(2);
            }
            service_settings_request(&endpoint, RequestKind::SetSleepSettings { patch })
        }
        Some("service-agent-list") => {
            let (Some(endpoint), Some(idle), Some(retention), None) =
                (args.next(), args.next(), args.next(), args.next())
            else {
                eprintln!(
                    "usage: sidepulse-next service-agent-list ENDPOINT IDLE_MINUTES RETENTION_HOURS"
                );
                return ExitCode::from(2);
            };
            let (Ok(idle), Ok(retention)) = (idle.parse::<f64>(), retention.parse::<f64>()) else {
                eprintln!("sidepulse-next: agent-list durations must be numbers");
                return ExitCode::from(2);
            };
            let patch = sidepulse_core::AgentListSettingsPatch {
                idle_timeout_seconds: Some(idle * 60.0),
                recent_session_retention_seconds: Some(retention * 3600.0),
            };
            if let Err(error) = patch.validate() {
                eprintln!("sidepulse-next: {error}");
                return ExitCode::from(2);
            }
            service_settings_request(&endpoint, RequestKind::SetAgentListSettings { patch })
        }
        Some("service-lid-duration") => {
            let (Some(endpoint), Some(state), Some(seconds), None) =
                (args.next(), args.next(), args.next(), args.next())
            else {
                eprintln!(
                    "usage: sidepulse-next service-lid-duration ENDPOINT lid_open|lid_closed SECONDS"
                );
                return ExitCode::from(2);
            };
            let Ok(seconds) = seconds.parse() else {
                eprintln!("sidepulse-next: invalid duration");
                return ExitCode::from(2);
            };
            if !matches!(state.as_str(), "lid_open" | "lid_closed") {
                eprintln!("sidepulse-next: invalid lid animation state");
                return ExitCode::from(2);
            }
            service_settings_request(
                &endpoint,
                RequestKind::SetLidAnimationTiming {
                    open_seconds: (state == "lid_open").then_some(seconds),
                    close_seconds: (state == "lid_closed").then_some(seconds),
                },
            )
        }
        Some("service-animation") => {
            let (Some(endpoint), Some(mode), Some(style)) = (args.next(), args.next(), args.next())
            else {
                eprintln!(
                    "usage: sidepulse-next service-animation ENDPOINT MODE STYLE [--program FILE]"
                );
                return ExitCode::from(2);
            };
            if !sidepulse_core::ANIMATION_STATES.contains(&mode.as_str()) {
                eprintln!("sidepulse-next: invalid animation state");
                return ExitCode::from(2);
            }
            let custom_program = match (args.next(), args.next(), args.next()) {
                (None, None, None) => None,
                (Some(flag), Some(path), None) if flag == "--program" => {
                    match fs::File::open(path).and_then(|file| {
                        let mut program = String::new();
                        file.take(65_537).read_to_string(&mut program)?;
                        if program.len() > 65_536 {
                            return Err(std::io::Error::new(
                                std::io::ErrorKind::InvalidInput,
                                "custom program exceeds 65536 bytes",
                            ));
                        }
                        Ok(program)
                    }) {
                        Ok(program) => Some(program),
                        Err(error) => {
                            eprintln!("sidepulse-next: {error}");
                            return ExitCode::FAILURE;
                        }
                    }
                }
                _ => {
                    eprintln!("sidepulse-next: use --program FILE for a custom animation");
                    return ExitCode::from(2);
                }
            };
            service_settings_request(
                &endpoint,
                RequestKind::SetAnimationState {
                    state: mode,
                    style,
                    custom_program,
                },
            )
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
                "usage: sidepulse-next <version | doctor [--json] | status [--json] | battery <status | configure> | settings --endpoint ENDPOINT | virtual-display --endpoint ENDPOINT | link [RELAY_CODE] [--server ORIGIN] [--config PATH | --endpoint ENDPOINT] | hook-log --provider PROVIDER --log PATH | agent-monitor <doctor | status | hook-log | install | uninstall> | service-status ENDPOINT | service-settings ENDPOINT | open-session ENDPOINT AGENT_ID [app|terminal|vscode] [--dry-run] | service-session-preference ENDPOINT PROVIDER ACTION [ORIGIN] | service-session-terminal ENDPOINT TERMINAL [CUSTOM_PATH] | service-power ENDPOINT | service-devices ENDPOINT | service-select ENDPOINT DEVICE_ROOT | service-brightness ENDPOINT 0-255 | service-display ENDPOINT agent|battery|custom | service-sleep-policy ENDPOINT never|agents|always | service-sleep-safeguard ENDPOINT 0-100 | service-agent-list ENDPOINT IDLE_MINUTES RETENTION_HOURS | animation-profile ENDPOINT OPERATION | animation-asset ENDPOINT OPERATION | service-lid-duration ENDPOINT lid_open|lid_closed SECONDS | service-animation ENDPOINT MODE STYLE [--program FILE] | service-history ENDPOINT | service-history-timeframe ENDPOINT 1|6|12|24|48 | service-virtual-frame ENDPOINT | service-transcript ENDPOINT codex|claude on|off | inspect-log PROVIDER JSONL_PATH [ISO_TIMESTAMP]>"
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
    if let Err(message) = request.validate() {
        eprintln!("sidepulse-next: {message}");
        return ExitCode::from(2);
    }
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

fn session_action(value: &str) -> Option<sidepulse_core::SessionAction> {
    sidepulse_core::SessionAction::ALL
        .into_iter()
        .find(|action| action.key() == value)
}

fn run_open_session(mut args: impl Iterator<Item = String>) -> ExitCode {
    let (Some(endpoint), Some(agent_id)) = (args.next(), args.next()) else {
        eprintln!(
            "usage: sidepulse-next open-session ENDPOINT AGENT_ID [app|terminal|vscode] [--dry-run]"
        );
        return ExitCode::from(2);
    };
    let mut action = None;
    let mut dry_run = false;
    for argument in args {
        if argument == "--dry-run" && !dry_run {
            dry_run = true;
        } else if action.is_none()
            && let Some(selected) = session_action(&argument)
        {
            action = Some(selected);
        } else {
            eprintln!("sidepulse-next: invalid session action or extra argument: {argument}");
            return ExitCode::from(2);
        }
    }
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind: RequestKind::SessionTargets { agent_id, action },
    };
    let result = (|| -> Result<(), String> {
        request.validate().map_err(str::to_owned)?;
        let reply: ServerMessage =
            sidepulse_ipc::request(&endpoint, &request, Duration::from_secs(2))
                .map_err(|error| format!("service unavailable: {error}"))?;
        if reply.version != PROTOCOL_VERSION || reply.request_id != Some(1) {
            return Err("invalid service response".into());
        }
        match reply.payload {
            ServerPayload::SessionTargets {
                options,
                selected,
                terminal,
                custom_terminal_path,
            } => {
                let option = options
                    .iter()
                    .find(|option| Some(option.action) == selected)
                    .ok_or("this session has no available opener")?;
                if dry_run {
                    println!(
                        "{}",
                        serde_json::to_string_pretty(&serde_json::json!({
                            "action": option.action, "target": option.target,
                            "terminal": terminal, "custom_terminal_path": custom_terminal_path
                        }))
                        .expect("session target serializes")
                    );
                } else {
                    sidepulse_platform::open_session(
                        &option.target,
                        &terminal,
                        &custom_terminal_path,
                    )
                    .map_err(|error| error.to_string())?;
                }
                Ok(())
            }
            ServerPayload::Error { message, .. } => Err(message),
            _ => Err("unexpected service response".into()),
        }
    })();
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("sidepulse-next: {error}");
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

fn read_bounded(path: &str, limit: u64) -> Result<String, String> {
    let mut text = String::new();
    fs::File::open(path)
        .map_err(|error| error.to_string())?
        .take(limit + 1)
        .read_to_string(&mut text)
        .map_err(|error| error.to_string())?;
    if text.len() as u64 > limit {
        Err("input file is too large".into())
    } else {
        Ok(text)
    }
}

fn run_animation_library(command: &str, args: impl Iterator<Item = String>) -> ExitCode {
    use sidepulse_core::AnimationLibraryEdit;
    let args: Vec<_> = args.collect();
    let parse = || -> Result<(&str, RequestKind), String> {
        let values: Vec<_> = args.iter().map(String::as_str).collect();
        let (endpoint, kind) = match (command, values.as_slice()) {
            (_, [endpoint, "list"]) => (*endpoint, RequestKind::AnimationLibrary),
            ("animation-profile", [endpoint, "save", name]) => (
                *endpoint,
                RequestKind::EditAnimationLibrary {
                    edit: AnimationLibraryEdit::SaveProfile {
                        id: None,
                        name: (*name).into(),
                    },
                },
            ),
            ("animation-profile", [endpoint, "save", name, id]) => (
                *endpoint,
                RequestKind::EditAnimationLibrary {
                    edit: AnimationLibraryEdit::SaveProfile {
                        id: Some((*id).into()),
                        name: (*name).into(),
                    },
                },
            ),
            ("animation-profile", [endpoint, "apply", id]) => (
                *endpoint,
                RequestKind::EditAnimationLibrary {
                    edit: AnimationLibraryEdit::ApplyProfile { id: (*id).into() },
                },
            ),
            ("animation-profile", [endpoint, "delete", id]) => (
                *endpoint,
                RequestKind::EditAnimationLibrary {
                    edit: AnimationLibraryEdit::DeleteProfile { id: (*id).into() },
                },
            ),
            ("animation-profile", [endpoint, "export"]) => {
                (*endpoint, RequestKind::ExportAnimationProfile { id: None })
            }
            ("animation-profile", [endpoint, "export", id]) => (
                *endpoint,
                RequestKind::ExportAnimationProfile {
                    id: Some((*id).into()),
                },
            ),
            ("animation-profile", [endpoint, "import", path]) => {
                let document = serde_json::from_str(&read_bounded(path, 900_000)?)
                    .map_err(|error| format!("invalid profile JSON: {error}"))?;
                (
                    *endpoint,
                    RequestKind::EditAnimationLibrary {
                        edit: AnimationLibraryEdit::ImportProfile { document },
                    },
                )
            }
            ("animation-asset", [endpoint, "save", name, path]) => (
                *endpoint,
                RequestKind::EditAnimationLibrary {
                    edit: AnimationLibraryEdit::SaveAnimation {
                        id: None,
                        name: (*name).into(),
                        program: read_bounded(path, 65536)?,
                    },
                },
            ),
            ("animation-asset", [endpoint, "save", name, path, id]) => (
                *endpoint,
                RequestKind::EditAnimationLibrary {
                    edit: AnimationLibraryEdit::SaveAnimation {
                        id: Some((*id).into()),
                        name: (*name).into(),
                        program: read_bounded(path, 65536)?,
                    },
                },
            ),
            ("animation-asset", [endpoint, "delete", id]) => (
                *endpoint,
                RequestKind::EditAnimationLibrary {
                    edit: AnimationLibraryEdit::DeleteAnimation { id: (*id).into() },
                },
            ),
            _ => {
                return Err(format!(
                    "usage: sidepulse-next {command} ENDPOINT <list | save NAME {} | {}delete ID>",
                    if command == "animation-profile" {
                        "[ID]"
                    } else {
                        "FILE [ID]"
                    },
                    if command == "animation-profile" {
                        "apply ID | import FILE | export [ID] | "
                    } else {
                        ""
                    }
                ));
            }
        };
        Ok((endpoint, kind))
    };
    let (endpoint, kind) = match parse() {
        Ok(value) => value,
        Err(error) => {
            eprintln!("sidepulse-next: {error}");
            return ExitCode::from(2);
        }
    };
    if matches!(kind, RequestKind::EditAnimationLibrary { .. }) {
        return service_settings_request(endpoint, kind);
    }
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind,
    };
    let result = (|| -> Result<(), String> {
        request.validate().map_err(str::to_owned)?;
        let reply: ServerMessage =
            sidepulse_ipc::request(endpoint, &request, Duration::from_secs(3))
                .map_err(|error| error.to_string())?;
        if reply.version != PROTOCOL_VERSION || reply.request_id != Some(1) {
            return Err("invalid service response".into());
        }
        let value = match reply.payload {
            ServerPayload::AnimationLibrary { library } => serde_json::to_value(library),
            ServerPayload::AnimationProfileDocument { document } => serde_json::to_value(document),
            ServerPayload::Error { message, .. } => return Err(message),
            _ => return Err("unexpected service response".into()),
        }
        .map_err(|error| error.to_string())?;
        println!(
            "{}",
            serde_json::to_string_pretty(&value).expect("animation JSON serializes")
        );
        Ok(())
    })();
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("sidepulse-next: {error}");
            ExitCode::FAILURE
        }
    }
}
