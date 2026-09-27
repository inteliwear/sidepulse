use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use sidepulse_relay::{
    DEFAULT_BRIDGE_SERVER, clean_channel, load_config, normalize_server, save_config,
};

pub fn run_link(args: impl Iterator<Item = String>) -> ExitCode {
    let mut code = None;
    let mut server = None;
    let mut config_path = None;
    let mut args = args.peekable();
    while let Some(arg) = args.next() {
        match arg.as_str() {
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
    let mut config = match load_config(&path, &host) {
        Ok(config) => config,
        Err(error) => {
            eprintln!("sidepulse-next link: {}: {error}", path.display());
            return ExitCode::FAILURE;
        }
    };
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
        if let Err(error) = save_config(&path, &config) {
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
    if let Err(error) = save_config(&path, &config) {
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
    eprintln!("usage: sidepulse-next link [RELAY_CODE] [--server ORIGIN] [--config PATH]");
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
