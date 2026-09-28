use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use sidepulse_installer::{Platform, StagePlan, smoke_stage};

fn main() -> ExitCode {
    let arguments = env::args().skip(1).collect::<Vec<_>>();
    if arguments
        .first()
        .is_some_and(|flag| flag == "--smoke-stage")
    {
        if arguments.len() != 2 {
            eprintln!("usage: sidepulse-next-stage --smoke-stage DIR");
            return ExitCode::from(2);
        }
        return match smoke_stage(&PathBuf::from(&arguments[1])) {
            Ok(()) => {
                println!("Preview service passed the isolated startup check.");
                ExitCode::SUCCESS
            }
            Err(error) => {
                eprintln!("sidepulse-next-stage: {error}");
                ExitCode::FAILURE
            }
        };
    }
    if arguments.first().is_some_and(|flag| {
        matches!(
            flag.as_str(),
            "--package"
                | "--seal-package"
                | "--verify-package"
                | "--archive-package"
                | "--sign-package"
                | "--macos-pkg"
        )
    }) {
        return package_command(&arguments);
    }
    let mut source_dir = None;
    let mut stage_dir = None;
    let mut dry_run = false;
    let mut args = arguments.into_iter();
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--source-dir" | "--stage-dir" => {
                let Some(value) = args.next().filter(|value| !value.starts_with('-')) else {
                    eprintln!("sidepulse-next-stage: {flag} needs a directory");
                    return ExitCode::from(2);
                };
                if flag == "--source-dir" {
                    source_dir = Some(PathBuf::from(value));
                } else {
                    stage_dir = Some(PathBuf::from(value));
                }
            }
            "--dry-run" => dry_run = true,
            _ => {
                eprintln!("sidepulse-next-stage: unknown argument: {flag}");
                return ExitCode::from(2);
            }
        }
    }
    let Some(stage_dir) = stage_dir else {
        eprintln!("usage: sidepulse-next-stage --stage-dir DIR [--source-dir DIR] [--dry-run]");
        return ExitCode::from(2);
    };
    let source_dir = match source_dir {
        Some(source_dir) => source_dir,
        None => match env::current_exe() {
            Ok(path) => path.parent().expect("executable has parent").to_path_buf(),
            Err(error) => {
                eprintln!("sidepulse-next-stage: {error}");
                return ExitCode::FAILURE;
            }
        },
    };
    let platform = match Platform::current() {
        Ok(platform) => platform,
        Err(error) => {
            eprintln!("sidepulse-next-stage: {error}");
            return ExitCode::FAILURE;
        }
    };
    let plan = match StagePlan::new(&source_dir, &stage_dir, platform) {
        Ok(plan) => plan,
        Err(error) => {
            eprintln!("sidepulse-next-stage: {error}");
            return ExitCode::FAILURE;
        }
    };
    let manifest = if dry_run {
        plan.manifest()
    } else {
        match plan.stage() {
            Ok(manifest) => manifest,
            Err(error) => {
                eprintln!("sidepulse-next-stage: {error}");
                return ExitCode::FAILURE;
            }
        }
    };
    println!(
        "{}",
        serde_json::to_string_pretty(manifest).expect("manifest serializes")
    );
    ExitCode::SUCCESS
}

fn package_command(arguments: &[String]) -> ExitCode {
    use sidepulse_installer::package;
    let result = (|| -> std::io::Result<serde_json::Value> {
        if arguments
            .first()
            .is_some_and(|command| command == "--macos-pkg")
        {
            let mut args = arguments.iter().skip(1);
            let root = args
                .next()
                .ok_or_else(|| std::io::Error::other("provide a package directory"))?;
            let destination = args
                .next()
                .ok_or_else(|| std::io::Error::other("provide a PKG output path"))?;
            let mut identity = None;
            let mut dry_run = false;
            while let Some(arg) = args.next() {
                match arg.as_str() {
                    "--dry-run" => dry_run = true,
                    "--installer-identity" => {
                        identity = Some(
                            args.next()
                                .ok_or_else(|| {
                                    std::io::Error::other("provide an installer signing identity")
                                })?
                                .as_str(),
                        )
                    }
                    _ => return Err(std::io::Error::other("unknown macOS installer option")),
                }
            }
            let plan = package::MacInstallerPlan::new(
                std::path::Path::new(root),
                std::path::Path::new(destination),
                identity,
            )?;
            if !dry_run {
                plan.apply()?;
            }
            return Ok(serde_json::to_value(plan)?);
        }
        if arguments
            .first()
            .is_some_and(|command| command == "--sign-package")
        {
            let mut args = arguments.iter().skip(1);
            let root = args
                .next()
                .ok_or_else(|| std::io::Error::other("provide a package directory"))?;
            let identity = args
                .next()
                .ok_or_else(|| std::io::Error::other("provide a signing identity"))?;
            let mut timestamp = None;
            let mut dry_run = false;
            while let Some(arg) = args.next() {
                match arg.as_str() {
                    "--dry-run" => dry_run = true,
                    "--timestamp-url" => {
                        timestamp = Some(
                            args.next()
                                .ok_or_else(|| std::io::Error::other("provide a timestamp URL"))?
                                .as_str(),
                        )
                    }
                    _ => return Err(std::io::Error::other("unknown package signing option")),
                }
            }
            let plan = package::SigningPlan::new(std::path::Path::new(root), identity, timestamp)?;
            return if dry_run {
                Ok(serde_json::to_value(plan)?)
            } else {
                Ok(serde_json::to_value(plan.apply()?)?)
            };
        }
        match arguments {
            [command, root] if command == "--verify-package" => Ok(serde_json::to_value(
                package::verify(std::path::Path::new(root))?,
            )?),
            [command, root, version] if command == "--seal-package" => Ok(serde_json::to_value(
                package::seal(std::path::Path::new(root), version)?,
            )?),
            [command, root, archive] if command == "--archive-package" => {
                package::archive(std::path::Path::new(root), std::path::Path::new(archive))?;
                Ok(serde_json::json!({"archive":archive}))
            }
            [command, source, destination, version] if command == "--package" => {
                Ok(serde_json::to_value(package::build(
                    std::path::Path::new(source),
                    std::path::Path::new(destination),
                    version,
                )?)?)
            }
            _ => Err(std::io::Error::other(
                "Usage: sidepulse-next-stage --package SOURCE_DIR PACKAGE_DIR VERSION | --seal-package PACKAGE_DIR VERSION | --verify-package PACKAGE_DIR | --archive-package PACKAGE_DIR ZIP",
            )),
        }
    })();
    match result {
        Ok(value) => {
            println!("{}", serde_json::to_string_pretty(&value).unwrap());
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("sidepulse-next-stage: {error}");
            ExitCode::FAILURE
        }
    }
}
