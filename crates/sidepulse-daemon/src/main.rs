use std::path::PathBuf;
use std::process::ExitCode;

fn main() -> ExitCode {
    let mut args = std::env::args().skip(1);
    let Some(endpoint) = args.next() else {
        eprintln!(
            "usage: sidepulse-next-service ENDPOINT [--log PROVIDER PATH]... [--transcript codex|claude DIR]... [--device PATH | --auto-device] [--brightness 0-255] [--state PATH] [--history PATH] [--settings PATH] [--relay-config PATH] [--power-control | --mock-power JSON_PATH]"
        );
        return ExitCode::from(2);
    };
    let mut logs = Vec::new();
    let mut device = None;
    let mut auto_device = false;
    let mut state = None;
    let mut history = None;
    let mut settings = None;
    let mut relay_config = None;
    let mut power_control = false;
    let mut power_observation = None;
    let mut brightness = None;
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--log" => {
                let (Some(provider), Some(path)) = (args.next(), args.next()) else {
                    eprintln!(
                        "usage: sidepulse-next-service ENDPOINT [--log PROVIDER PATH]... [--transcript codex|claude DIR]... [--device PATH | --auto-device] [--brightness 0-255] [--state PATH] [--history PATH] [--settings PATH] [--relay-config PATH] [--power-control | --mock-power JSON_PATH]"
                    );
                    return ExitCode::from(2);
                };
                logs.push((provider, PathBuf::from(path)));
            }
            "--transcript" => {
                let (Some(provider), Some(path)) = (args.next(), args.next()) else {
                    eprintln!(
                        "sidepulse-next-service: --transcript needs a provider and directory"
                    );
                    return ExitCode::from(2);
                };
                if provider != "codex" && provider != "claude" {
                    eprintln!(
                        "sidepulse-next-service: transcript provider must be codex or claude"
                    );
                    return ExitCode::from(2);
                }
                logs.push((format!("{provider}-transcripts"), PathBuf::from(path)));
            }
            "--device" => {
                let Some(path) = args.next() else {
                    eprintln!("sidepulse-next-service: --device requires a path");
                    return ExitCode::from(2);
                };
                device = Some(PathBuf::from(path));
            }
            "--auto-device" => auto_device = true,
            "--state" => {
                let Some(path) = args.next() else {
                    eprintln!("sidepulse-next-service: --state requires a path");
                    return ExitCode::from(2);
                };
                state = Some(PathBuf::from(path));
            }
            "--history" => {
                let Some(path) = args.next() else {
                    eprintln!("sidepulse-next-service: --history requires a path");
                    return ExitCode::from(2);
                };
                history = Some(PathBuf::from(path));
            }
            "--settings" => {
                let Some(path) = args.next() else {
                    eprintln!("sidepulse-next-service: --settings requires a path");
                    return ExitCode::from(2);
                };
                settings = Some(PathBuf::from(path));
            }
            "--relay-config" => {
                let Some(path) = args.next() else {
                    eprintln!("sidepulse-next-service: --relay-config requires a path");
                    return ExitCode::from(2);
                };
                relay_config = Some(PathBuf::from(path));
            }
            "--power-control" => power_control = true,
            "--mock-power" => {
                let Some(path) = args.next() else {
                    eprintln!("sidepulse-next-service: --mock-power requires a JSON path");
                    return ExitCode::from(2);
                };
                power_observation = Some(PathBuf::from(path));
            }
            "--brightness" => {
                let Some(value) = args.next().and_then(|value| value.parse::<u8>().ok()) else {
                    eprintln!("sidepulse-next-service: brightness must be 0-255");
                    return ExitCode::from(2);
                };
                brightness = Some(value);
            }
            _ => {
                eprintln!(
                    "usage: sidepulse-next-service ENDPOINT [--log PROVIDER PATH]... [--transcript codex|claude DIR]... [--device PATH | --auto-device] [--brightness 0-255] [--state PATH] [--history PATH] [--settings PATH] [--relay-config PATH] [--power-control | --mock-power JSON_PATH]"
                );
                return ExitCode::from(2);
            }
        }
    }
    if auto_device && (device.is_some() || brightness.is_some()) {
        eprintln!(
            "sidepulse-next-service: --auto-device cannot be combined with --device or --brightness"
        );
        return ExitCode::from(2);
    }
    if let Err(error) = sidepulse_daemon::run_with_options(
        &endpoint,
        sidepulse_daemon::RunOptions {
            logs: &logs,
            device: device.as_deref().map(|path| (path, brightness)),
            latest_state_path: state.as_deref(),
            settings_path: settings.as_deref(),
            auto_device,
            relay_config_path: relay_config.as_deref(),
            power_control,
            power_observation_path: power_observation.as_deref(),
            history_path: history.as_deref(),
        },
    ) {
        eprintln!("sidepulse-next-service: {error}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}
