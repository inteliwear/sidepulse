//! Native setup operations shared by presentation clients and the CLI.
use crate::{
    Platform, read_stage,
    startup::{Job, Operation, StartupPlan},
};
use sidepulse_core::{HookSetupStatus, SessionTarget};
use std::{
    io,
    path::{Path, PathBuf},
};

/// Recover management context from a verified staged application while its
/// service is stopped. Provider hooks remain service-owned.
pub fn context_from_executable(
    executable: &Path,
    endpoint: &str,
) -> io::Result<Option<HookSetupStatus>> {
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .ok_or_else(|| io::Error::other("home directory is unavailable"))?;
    let directory = match Platform::current()? {
        Platform::Macos => Some(home.join("Library/LaunchAgents")),
        Platform::Linux => Some(
            std::env::var_os("XDG_CONFIG_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| home.join(".config"))
                .join("systemd/user"),
        ),
        Platform::Windows => std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .map(|path| path.join("SidePulse/Startup")),
    };
    context_at(executable, endpoint, &home, directory.as_deref())
}

fn context_at(
    executable: &Path,
    endpoint: &str,
    home: &Path,
    directory: Option<&Path>,
) -> io::Result<Option<HookSetupStatus>> {
    for ancestor in executable.ancestors().skip(1).take(6) {
        if ancestor.join("manifest.json").exists() {
            let manifest = read_stage(ancestor)?;
            if manifest.endpoint != endpoint || manifest.platform != Platform::current()? {
                return Err(io::Error::other(
                    "setup bundle does not match the selected service",
                ));
            }
            return Ok(Some(HookSetupStatus {
                configured: false,
                stage_dir: Some(manifest.stage_dir.to_string_lossy().into_owned()),
                startup_directory: directory.map(|path| path.to_string_lossy().into_owned()),
                home: home.to_string_lossy().into_owned(),
                providers: vec![],
            }));
        }
    }
    Ok(None)
}

pub fn sleep_helper_status(user: &str) -> io::Result<bool> {
    if Platform::current()? != Platform::Macos {
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "closed-lid setup is available only on macOS",
        ));
    }
    sidepulse_helpers::sleep_helper::plan(
        Path::new(sidepulse_helpers::sleep_helper::DEFAULT_PATH),
        user,
        sidepulse_helpers::sleep_helper::Operation::Install,
    )
    .map(|plan| plan.installed)
}

pub fn startup_plan(
    context: &HookSetupStatus,
    endpoint: &str,
    job: Job,
    operation: Operation,
    user: &str,
) -> io::Result<StartupPlan> {
    let stage = context
        .stage_dir
        .as_deref()
        .ok_or_else(|| io::Error::other("stage the native package before configuring startup"))?;
    let manifest = read_stage(Path::new(stage))?;
    if manifest.endpoint != endpoint || manifest.platform != Platform::current()? {
        return Err(io::Error::other(
            "startup bundle does not match the connected service",
        ));
    }
    let directory = context
        .startup_directory
        .as_deref()
        .ok_or_else(|| io::Error::other("the startup directory is unavailable"))?;
    // Login registration and immediate start are separate user actions.
    StartupPlan::new(
        &manifest,
        job,
        operation,
        operation == Operation::Start,
        Path::new(directory),
        user,
    )
}

pub fn manage_startup(
    context: &HookSetupStatus,
    endpoint: &str,
    job: Job,
    operation: Operation,
    dry_run: bool,
) -> io::Result<String> {
    let user = crate::startup::current_user(Platform::current()?)?;
    let plan = startup_plan(context, endpoint, job, operation, &user)?;
    if dry_run {
        return serde_json::to_string_pretty(&plan).map_err(io::Error::other);
    }
    let result = plan.apply()?;
    let label = match job {
        Job::Service => "Monitor",
        Job::Tray => "Status bar",
        Job::SdGuard => "SD eject protection",
    };
    if operation == Operation::Status {
        Ok(format!(
            "{label}: {} · {}",
            if result.installed {
                "Installed"
            } else {
                "Not installed"
            },
            match result.running {
                Some(true) => "Running",
                Some(false) => "Stopped",
                None => "Status unavailable",
            }
        ))
    } else {
        Ok(format!(
            "{label}: {}",
            match operation {
                Operation::Install => "Enabled at login",
                Operation::Start => "Started",
                Operation::Stop => "Stopped",
                Operation::Uninstall => "Disabled at login",
                Operation::Status => unreachable!(),
            }
        ))
    }
}

pub fn sleep_helper_target(
    context: &HookSetupStatus,
    endpoint: &str,
    install: bool,
    user: &str,
) -> io::Result<SessionTarget> {
    if Platform::current()? != Platform::Macos {
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "closed-lid setup is available only on macOS",
        ));
    }
    // The helper validates this name again before any administrator operation.
    if user.is_empty()
        || !user
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
    {
        return Err(io::Error::other("invalid setup user"));
    }
    let stage = context.stage_dir.as_deref().ok_or_else(|| {
        io::Error::other("stage the native package before configuring sleep prevention")
    })?;
    let manifest = read_stage(Path::new(stage))?;
    if manifest.endpoint != endpoint {
        return Err(io::Error::other(
            "setup bundle does not match the connected service",
        ));
    }
    Ok(SessionTarget::Terminal {
        executable: "/usr/bin/sudo".into(),
        args: vec![
            manifest.binaries[0].to_string_lossy().into_owned(),
            "status-bar".into(),
            if install {
                "install-sleep-helper"
            } else {
                "uninstall-sleep-helper"
            }
            .into(),
            "--user".into(),
            user.into(),
        ],
        cwd: context.home.clone(),
        hints: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn management_binds_plans_to_the_connected_preview_without_starting_jobs() {
        let temp = tempfile::tempdir().unwrap();
        let source = temp.path().join("source");
        std::fs::create_dir(&source).unwrap();
        let platform = Platform::current().unwrap();
        for name in crate::BINARIES {
            std::fs::write(
                source.join(format!("{name}{}", platform.executable_suffix())),
                b"fixture",
            )
            .unwrap();
        }
        let stage = temp.path().join("preview");
        let plan = crate::StagePlan::new(&source, &stage, platform).unwrap();
        let manifest = plan.stage().unwrap();
        let startup_directory = temp.path().join("login");
        let context = HookSetupStatus {
            configured: true,
            stage_dir: Some(stage.to_string_lossy().into_owned()),
            startup_directory: Some(startup_directory.to_string_lossy().into_owned()),
            home: temp.path().to_string_lossy().into_owned(),
            providers: vec![],
        };
        let user = match platform {
            Platform::Macos => "501",
            Platform::Linux => "fixture",
            Platform::Windows => "S-1-5-21-123",
        };
        let result = startup_plan(
            &context,
            &manifest.endpoint,
            Job::Tray,
            Operation::Install,
            user,
        )
        .unwrap();
        let recovered = context_at(
            &manifest.binaries[4],
            &manifest.endpoint,
            temp.path(),
            Some(&startup_directory),
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            Path::new(recovered.stage_dir.as_deref().unwrap())
                .canonicalize()
                .unwrap(),
            stage.canonicalize().unwrap()
        );
        assert!(!recovered.configured);
        assert!(
            context_at(
                &manifest.binaries[4],
                "another-endpoint",
                temp.path(),
                Some(&startup_directory)
            )
            .is_err()
        );
        let start = startup_plan(
            &recovered,
            &manifest.endpoint,
            Job::Service,
            Operation::Start,
            user,
        )
        .unwrap();
        assert!(start.start);
        assert!(start.commands.iter().any(|command| {
            command
                .args
                .iter()
                .any(|arg| matches!(arg.as_str(), "bootstrap" | "--now" | "/Run"))
        }));
        assert!(!result.start);
        assert!(result.path.starts_with(startup_directory));
        assert!(!result.commands.iter().any(|command| {
            command
                .args
                .iter()
                .any(|arg| matches!(arg.as_str(), "bootstrap" | "--now" | "/Run"))
        }));
        assert!(
            startup_plan(
                &context,
                "another-endpoint",
                Job::Service,
                Operation::Install,
                user
            )
            .is_err()
        );
        if platform == Platform::Macos {
            let SessionTarget::Terminal {
                executable,
                args,
                hints,
                ..
            } = sleep_helper_target(&context, &manifest.endpoint, true, "fixture").unwrap()
            else {
                panic!("missing administrator target");
            };
            assert_eq!(executable, "/usr/bin/sudo");
            assert_eq!(args[2], "install-sleep-helper");
            assert!(hints.is_none());
            assert!(
                sleep_helper_target(&context, &manifest.endpoint, true, "user;escape").is_err()
            );
        }
        assert!(!temp.path().join("login").exists());
    }
}
