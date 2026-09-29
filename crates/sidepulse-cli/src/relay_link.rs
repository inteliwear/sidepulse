use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use sidepulse_relay::{DEFAULT_BRIDGE_SERVER, RelayConfigStore, clean_channel, normalize_server};

pub fn run_link(args: impl Iterator<Item = String>) -> ExitCode {
    let mut code = None;
    let mut server = None;
    let mut config_path = None;
    let mut endpoint = None;
    let mut args = args.peekable();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--endpoint" => {
                let Some(value) = args.next() else {
                    return usage();
                };
                endpoint = Some(value);
            }
            "--config" => {
                let Some(value) = args.next() else {
                    return usage();
                };
                config_path = Some(PathBuf::from(value));
            }
            "--server" => {
                let Some(value) = args.next() else {
                    return usage();
                };
                server = Some(value);
            }
            "--" => {
                if code.is_some() {
                    return usage();
                }
                let Some(value) = args.next() else {
                    return usage();
                };
                code = Some(value);
                if args.next().is_some() {
                    return usage();
                }
            }
            _ if code.is_some()
                || (arg.starts_with('-') && (arg.len() != 22 || clean_channel(&arg).is_err())) =>
            {
                return usage();
            }
            _ => code = Some(arg),
        }
    }
    if let Some(endpoint) = endpoint {
        if config_path.is_some() {
            return usage();
        }
        return run_service_link(&endpoint, code, server);
    }
    let path = match config_path.or_else(default_config_path) {
        Some(path) => path,
        None => {
            eprintln!("sidepulse-next link: cannot find the home directory; use --config PATH");
            return ExitCode::FAILURE;
        }
    };
    let host = env::var("HOSTNAME")
        .or_else(|_| env::var("COMPUTERNAME"))
        .unwrap_or_else(|_| "Remote computer".into());
    let mut store = match RelayConfigStore::load(&path, &host) {
        Ok(store) => store,
        Err(error) => {
            eprintln!("sidepulse-next link: {}: {error}", path.display());
            return ExitCode::FAILURE;
        }
    };
    let mut config = store.config().clone();
    if let Some(code) = code {
        let target_server = server
            .or_else(|| env::var("SIDEPULSE_SERVER").ok())
            .unwrap_or_else(|| config.server.clone());
        config = match config.with_outbound_channel(&code, &target_server) {
            Ok(config) => config,
            Err(error) => {
                eprintln!("sidepulse-next link: {error}");
                return ExitCode::from(2);
            }
        };
        if let Err(error) = store.save(config.clone()) {
            eprintln!("sidepulse-next link: {}: {error}", path.display());
            return ExitCode::FAILURE;
        }
        println!("Relay link saved to {}.", path.display());
        return ExitCode::SUCCESS;
    }
    if let Some(server) = server {
        config.server = match normalize_server(&server) {
            Ok(server) => server,
            Err(error) => {
                eprintln!("sidepulse-next link: {error}");
                return ExitCode::from(2);
            }
        };
    }
    config = match config.with_receiver_channel() {
        Ok(config) => config,
        Err(error) => {
            eprintln!("sidepulse-next link: could not create a relay code: {error}");
            return ExitCode::FAILURE;
        }
    };
    if let Err(error) = store.save(config.clone()) {
        eprintln!("sidepulse-next link: {}: {error}", path.display());
        return ExitCode::FAILURE;
    }
    println!("Run this on the other computer:");
    if config.server == DEFAULT_BRIDGE_SERVER {
        println!("  sidepulse link {}", config.receiver_channel);
    } else {
        println!(
            "  sidepulse link {} --server {}",
            config.receiver_channel, config.server
        );
    }
    ExitCode::SUCCESS
}

fn usage() -> ExitCode {
    eprintln!(
        "usage: sidepulse-next link [RELAY_CODE] [--server ORIGIN] [--config PATH | --endpoint ENDPOINT]"
    );
    ExitCode::from(2)
}

fn default_config_path() -> Option<PathBuf> {
    let base = env::var_os("XDG_CONFIG_HOME")
        .or_else(|| {
            env::var_os("HOME").map(|home| PathBuf::from(home).join(".config").into_os_string())
        })
        .or_else(|| {
            env::var_os("USERPROFILE")
                .map(|home| PathBuf::from(home).join(".config").into_os_string())
        })?;
    Some(
        PathBuf::from(base)
            .join("sidepulse")
            .join("agent-monitor")
            .join("relay.json"),
    )
}

fn run_service_link(endpoint: &str, code: Option<String>, server: Option<String>) -> ExitCode {
    use sidepulse_core::{
        ClientRequest, PROTOCOL_VERSION, RelaySettingsPatch, RequestKind, ServerMessage,
        ServerPayload,
    };
    let receiving = code.is_none();
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind: RequestKind::SetRelaySettings {
            patch: RelaySettingsPatch {
                server: server.or_else(|| std::env::var("SIDEPULSE_SERVER").ok()),
                outbound_code: code,
                receiver_enabled: receiving.then_some(true),
                ..Default::default()
            },
        },
    };
    if let Err(message) = request.validate() {
        eprintln!("sidepulse-next link: {message}");
        return ExitCode::from(2);
    }
    let result = sidepulse_ipc::request::<_, ServerMessage>(
        endpoint,
        &request,
        std::time::Duration::from_secs(2),
    );
    match result {
        Ok(reply) if reply.version != PROTOCOL_VERSION || reply.request_id != Some(1) => {
            eprintln!("sidepulse-next link: invalid service response");
            ExitCode::FAILURE
        }
        Ok(ServerMessage {
            payload: ServerPayload::RelaySettings { settings },
            ..
        }) => {
            if receiving {
                println!("Run this on the other computer:");
                if settings.server == DEFAULT_BRIDGE_SERVER {
                    println!("  sidepulse-next link {}", settings.receiver_code);
                } else {
                    println!(
                        "  sidepulse-next link {} --server {}",
                        settings.receiver_code, settings.server
                    );
                }
            } else {
                println!("Relay link saved by the service.");
            }
            ExitCode::SUCCESS
        }
        Ok(ServerMessage {
            payload: ServerPayload::Error { message, .. },
            ..
        }) => {
            eprintln!("sidepulse-next link: {message}");
            ExitCode::FAILURE
        }
        Ok(_) => {
            eprintln!("sidepulse-next link: unexpected service response");
            ExitCode::FAILURE
        }
        Err(error) => {
            eprintln!("sidepulse-next link: service unavailable: {error}");
            ExitCode::FAILURE
        }
    }
}
