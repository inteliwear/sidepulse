use sidepulse_installer::{
    Platform, StagePlan, read_stage,
    startup::{Job, Operation, StartupPlan},
};
use std::{
    env, io,
    path::PathBuf,
    process::{Command, ExitCode},
};
fn fail(error: impl std::fmt::Display) -> ExitCode {
    eprintln!("sidepulse-next: {error}");
    ExitCode::FAILURE
}
pub fn run_setup(mut args: impl Iterator<Item = String>) -> ExitCode {
    let mut source = None;
    let mut stage = None;
    let mut dry_run = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--dry-run" => dry_run = true,
            "--source-dir" | "--stage-dir" => {
                let Some(value) = args.next() else {
                    return fail(format!("{arg} requires a directory"));
                };
                if arg == "--source-dir" {
                    source = Some(PathBuf::from(value));
                } else {
                    stage = Some(PathBuf::from(value));
                }
            }
            _ => return fail(format!("unknown setup option {arg}")),
        }
    }
    let result = (|| -> io::Result<serde_json::Value> {
        let stage = stage
            .ok_or_else(|| io::Error::other("provide --stage-dir DIR for the native preview"))?;
        let source = match source {
            Some(path) => path,
            None => env::current_exe()?.parent().unwrap().to_path_buf(),
        };
        let plan = StagePlan::new(&source, &stage, Platform::current()?)?;
        let manifest = if dry_run {
            plan.manifest()
        } else {
            plan.stage()?
        };
        Ok(serde_json::to_value(manifest)?)
    })();
    match result {
        Ok(value) => {
            println!("{}", serde_json::to_string_pretty(&value).unwrap());
            ExitCode::SUCCESS
        }
        Err(error) => fail(error),
    }
}
pub fn run_lifecycle(job: Job, mut args: impl Iterator<Item = String>) -> ExitCode {
    let operation = args.next().unwrap_or_else(|| "status".into());
    let mut stage = None;
    let mut directory = None;
    let mut user = None;
    let mut start = true;
    let mut dry_run = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--dry-run" => dry_run = true,
            "--no-start" => start = false,
            "--stage-dir" | "--startup-dir" | "--user" => {
                let Some(value) = args.next() else {
                    return fail(format!("{arg} requires a value"));
                };
                match arg.as_str() {
                    "--stage-dir" => stage = Some(PathBuf::from(value)),
                    "--startup-dir" => directory = Some(PathBuf::from(value)),
                    _ => user = Some(value),
                }
            }
            _ => return fail(format!("unknown startup option {arg}")),
        }
    }
    let result = (|| -> io::Result<serde_json::Value> {
        let stage = stage
            .ok_or_else(|| io::Error::other("provide --stage-dir DIR for the native preview"))?;
        let manifest = read_stage(&stage)?;
        if operation == "run" {
            let command = match job {
                Job::Service => manifest.service_command,
                Job::Tray => manifest.tray_command,
                Job::SdGuard => vec![manifest.binaries[7].to_string_lossy().into_owned()],
            };
            if dry_run {
                return Ok(serde_json::json!({"command":command}));
            }
            let status = Command::new(&command[0]).args(&command[1..]).status()?;
            if !status.success() {
                return Err(io::Error::other(format!("service exited: {status}")));
            }
            return Ok(serde_json::json!({"stopped":true}));
        }
        let operation = Operation::parse(&operation)?;
        if !start && !matches!(operation, Operation::Install) {
            return Err(io::Error::other(
                "--no-start is supported only for installation",
            ));
        }
        if user.is_none() && matches!(manifest.platform, Platform::Macos | Platform::Windows) {
            user = Some(current_user(manifest.platform)?);
        }
        let directory = match directory {
            Some(path) => path,
            None => default_directory(manifest.platform)?,
        };
        let plan = StartupPlan::new(
            &manifest,
            job,
            operation,
            start,
            &directory,
            user.as_deref().unwrap_or("current-user"),
        )?;
        if dry_run {
            Ok(serde_json::to_value(plan)?)
        } else {
            Ok(serde_json::to_value(plan.apply()?)?)
        }
    })();
    match result {
        Ok(value) => {
            println!("{}", serde_json::to_string_pretty(&value).unwrap());
            ExitCode::SUCCESS
        }
        Err(error) => fail(error),
    }
}
fn home() -> io::Result<PathBuf> {
    env::var_os("HOME")
        .or_else(|| env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .ok_or_else(|| io::Error::other("cannot determine the home directory"))
}
fn default_directory(platform: Platform) -> io::Result<PathBuf> {
    Ok(match platform {
        Platform::Macos => home()?.join("Library/LaunchAgents"),
        Platform::Linux => env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or(home()?.join(".config"))
            .join("systemd/user"),
        Platform::Windows => env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .ok_or_else(|| io::Error::other("LOCALAPPDATA is unavailable"))?
            .join("SidePulse/Startup"),
    })
}
fn current_user(platform: Platform) -> io::Result<String> {
    let output = if platform == Platform::Macos {
        Command::new("/usr/bin/id").arg("-u").output()?
    } else {
        Command::new("whoami.exe")
            .args(["/user", "/fo", "csv", "/nh"])
            .output()?
    };
    if !output.status.success() {
        return Err(io::Error::other("cannot determine startup user"));
    }
    let text = String::from_utf8_lossy(&output.stdout);
    if platform == Platform::Macos {
        Ok(text.trim().to_owned())
    } else {
        let user = text
            .trim()
            .rsplit(',')
            .next()
            .unwrap_or("")
            .trim_matches('"');
        if !user.starts_with("S-1-") {
            return Err(io::Error::other("cannot determine Windows user SID"));
        }
        Ok(user.to_owned())
    }
}
