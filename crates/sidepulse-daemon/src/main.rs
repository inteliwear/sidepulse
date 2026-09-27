use std::path::PathBuf;
use std::process::ExitCode;

fn main() -> ExitCode {
    let mut args = std::env::args().skip(1);
    let Some(endpoint) = args.next() else {
        eprintln!(
            "usage: sidepulse-next-service ENDPOINT [--log PROVIDER PATH]... [--device PATH] [--brightness 0-255] [--state PATH]"
        );
        return ExitCode::from(2);
    };
    let mut logs = Vec::new();
    let mut device = None;
    let mut state = None;
    let mut brightness = 255;
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--log" => {
                let (Some(provider), Some(path)) = (args.next(), args.next()) else {
                    eprintln!(
                        "usage: sidepulse-next-service ENDPOINT [--log PROVIDER PATH]... [--device PATH] [--brightness 0-255] [--state PATH]"
                    );
                    return ExitCode::from(2);
                };
                logs.push((provider, PathBuf::from(path)));
            }
            "--device" => {
                let Some(path) = args.next() else {
                    eprintln!("sidepulse-next-service: --device requires a path");
                    return ExitCode::from(2);
                };
                device = Some(PathBuf::from(path));
            }
            "--state" => {
                let Some(path) = args.next() else {
                    eprintln!("sidepulse-next-service: --state requires a path");
                    return ExitCode::from(2);
                };
                state = Some(PathBuf::from(path));
            }
            "--brightness" => {
                let Some(value) = args.next().and_then(|value| value.parse::<u8>().ok()) else {
                    eprintln!("sidepulse-next-service: brightness must be 0-255");
                    return ExitCode::from(2);
                };
                brightness = value;
            }
            _ => {
                eprintln!(
                    "usage: sidepulse-next-service ENDPOINT [--log PROVIDER PATH]... [--device PATH] [--brightness 0-255] [--state PATH]"
                );
                return ExitCode::from(2);
            }
        }
    }
    if let Err(error) = sidepulse_daemon::run_with_options(
        &endpoint,
        &logs,
        device.as_deref().map(|path| (path, brightness)),
        state.as_deref(),
    ) {
        eprintln!("sidepulse-next-service: {error}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}
