use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use sidepulse_installer::{Platform, StagePlan};

fn main() -> ExitCode {
    let mut source_dir = None;
    let mut stage_dir = None;
    let mut dry_run = false;
    let mut args = env::args().skip(1);
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
