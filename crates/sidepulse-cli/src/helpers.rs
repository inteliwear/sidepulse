use sidepulse_helpers::sleep_helper::{self, Operation};
use std::{
    env,
    path::PathBuf,
    process::{Command, ExitCode},
};
fn fail(error: impl std::fmt::Display) -> ExitCode {
    eprintln!("sidepulse-next: {error}");
    ExitCode::FAILURE
}
pub fn run_sd_guard(mut args: impl Iterator<Item = String>) -> ExitCode {
    let operation = args.next().unwrap_or_else(|| "check".into());
    if matches!(operation.as_str(), "--help" | "-h") {
        println!(
            "Usage: sidepulse sdejectguard check|run [--no-mount]\n       sidepulse sdejectguard install|start|stop|uninstall|status --stage-dir DIR [--dry-run]"
        );
        return ExitCode::SUCCESS;
    }
    if matches!(
        operation.as_str(),
        "install" | "start" | "stop" | "uninstall" | "status"
    ) {
        return crate::run_lifecycle(
            sidepulse_installer::startup::Job::SdGuard,
            std::iter::once(operation).chain(args),
        );
    }
    match operation.as_str() {
        "check" if args.next().is_none() => match sidepulse_helpers::check_sd_guard() {
            Ok(()) => {
                println!("SD eject protection is available.");
                ExitCode::SUCCESS
            }
            Err(error) => fail(error),
        },
        "run" => {
            let mut no_mount = false;
            for arg in args {
                match arg.as_str() {
                    "-n" | "--no-mount" => no_mount = true,
                    _ => return fail("usage: sdejectguard run [--no-mount]"),
                }
            }
            let executable = match env::current_exe() {
                Ok(path) => path.with_file_name(if cfg!(windows) {
                    "sidepulse-next-sd-guard.exe"
                } else {
                    "sidepulse-next-sd-guard"
                }),
                Err(error) => return fail(error),
            };
            let mut child = Command::new(executable);
            if no_mount {
                child.arg("--no-mount");
            }
            match child.status() {
                Ok(status) if status.success() => ExitCode::SUCCESS,
                Ok(_) => ExitCode::FAILURE,
                Err(error) => fail(error),
            }
        }
        _ => fail("usage: sidepulse-next sdejectguard check|run [--no-mount]"),
    }
}
pub fn run_status_bar(args: impl Iterator<Item = String>) -> ExitCode {
    let args = args.collect::<Vec<_>>();
    let mut args = args.into_iter();
    let operation = args.next().unwrap_or_else(|| "start".into());
    if matches!(operation.as_str(), "--help" | "-h") {
        println!(
            "Usage: sidepulse status-bar start|run [--endpoint ENDPOINT]\n       sidepulse status-bar install|start|stop|uninstall|status --stage-dir DIR [--dry-run]\n       sidepulse status-bar install-sleep-helper|uninstall-sleep-helper|sleep-helper-status [--dry-run]"
        );
        return ExitCode::SUCCESS;
    }
    let remaining = args.collect::<Vec<_>>();
    if matches!(
        operation.as_str(),
        "install" | "stop" | "uninstall" | "status"
    ) || remaining.iter().any(|arg| arg == "--stage-dir")
    {
        return crate::run_lifecycle(
            sidepulse_installer::startup::Job::Tray,
            std::iter::once(operation).chain(remaining),
        );
    }
    let mut args = remaining.into_iter();
    if matches!(operation.as_str(), "start" | "run") {
        let endpoint = match (args.next(), args.next(), args.next()) {
            (Some(flag), Some(endpoint), None) if flag == "--endpoint" => Some(endpoint),
            (None, None, None) => crate::preview_endpoint(),
            _ => None,
        };
        let Some(endpoint) = endpoint else {
            return fail("provide --endpoint ENDPOINT, or set SIDEPULSE_NEXT_ENDPOINT");
        };
        let executable = match env::current_exe() {
            Ok(path) => {
                let bundled = if cfg!(target_os = "macos") {
                    path.ancestors().take(3).map(|root|root.join("applications/SidePulse Tray.app/Contents/MacOS/sidepulse-next-tray")).find(|path|path.is_file())
                } else {
                    None
                };
                bundled.unwrap_or_else(|| {
                    path.with_file_name(if cfg!(windows) {
                        "sidepulse-next-tray.exe"
                    } else {
                        "sidepulse-next-tray"
                    })
                })
            }
            Err(error) => return fail(error),
        };
        let mut command = Command::new(executable);
        command.arg(endpoint);
        return if operation == "run" {
            match command.status() {
                Ok(status) if status.success() => ExitCode::SUCCESS,
                Ok(_) => ExitCode::FAILURE,
                Err(error) => fail(error),
            }
        } else {
            match command.spawn() {
                Ok(child) => {
                    println!("Status bar started ({})", child.id());
                    ExitCode::SUCCESS
                }
                Err(error) => fail(error),
            }
        };
    }
    if !matches!(
        operation.as_str(),
        "install-sleep-helper" | "uninstall-sleep-helper" | "sleep-helper-status"
    ) {
        return fail("unknown status-bar operation");
    }
    let mut path = PathBuf::from(sleep_helper::DEFAULT_PATH);
    let mut user = env::var("SUDO_USER")
        .or_else(|_| env::var("USER"))
        .or_else(|_| env::var("USERNAME"))
        .unwrap_or_default();
    let mut dry_run = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--dry-run" => dry_run = true,
            "--path" | "--user" => {
                let Some(value) = args.next() else {
                    return fail(format!("{arg} requires a value"));
                };
                if arg == "--path" {
                    path = value.into();
                } else {
                    user = value;
                }
            }
            _ => return fail(format!("unknown argument {arg}")),
        }
    }
    if operation == "sleep-helper-status" {
        println!(
            "{}",
            serde_json::json!({"supported":cfg!(target_os="macos"),"installed":std::fs::symlink_metadata(&path).is_ok_and(|metadata|metadata.is_file()),"path":path})
        );
        return ExitCode::SUCCESS;
    }
    let operation = if operation == "install-sleep-helper" {
        Operation::Install
    } else {
        Operation::Remove
    };
    let plan = match sleep_helper::plan(&path, &user, operation) {
        Ok(plan) => plan,
        Err(error) => return fail(error),
    };
    if !dry_run && let Err(error) = sleep_helper::apply(&plan) {
        return fail(error);
    }
    println!("{}", serde_json::to_string_pretty(&plan).unwrap());
    ExitCode::SUCCESS
}
