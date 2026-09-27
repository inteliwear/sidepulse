use std::path::PathBuf;
use std::process::ExitCode;

use serde_json::json;
use sidepulse_hook_config::{Action, plan_codex_hooks, plan_json_hooks};

pub fn run_hook_config(action: &str, args: impl Iterator<Item = String>) -> ExitCode {
    let action = match action {
        "install" => Action::Install,
        "uninstall" => Action::Uninstall,
        _ => return ExitCode::from(2),
    };
    let mut provider = None;
    let mut config = None;
    let mut log = None;
    let mut hook = None;
    let mut dry_run = false;
    let mut json_output = false;
    let mut args = args;
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--provider" => provider = args.next(),
            "--config" => config = args.next().map(PathBuf::from),
            "--log" => log = args.next().map(PathBuf::from),
            "--hook" => hook = args.next().map(PathBuf::from),
            "--dry-run" => dry_run = true,
            "--json" => json_output = true,
            _ => {
                eprintln!("sidepulse-next: unknown hook config argument: {flag}");
                return ExitCode::from(2);
            }
        }
    }
    let (Some(provider), Some(config), Some(log), Some(hook)) = (provider, config, log, hook)
    else {
        eprintln!(
            "usage: sidepulse-next agent-monitor <install | uninstall> --provider PROVIDER --config PATH --log PATH --hook EXECUTABLE [--dry-run] [--json]"
        );
        return ExitCode::from(2);
    };
    let plan = if provider == "codex" {
        plan_codex_hooks(&config, &log, &hook, action)
    } else {
        plan_json_hooks(&provider, &config, &log, &hook, action)
    };
    let plan = match plan {
        Ok(plan) => plan,
        Err(error) => {
            eprintln!("sidepulse-next: {error}");
            return ExitCode::FAILURE;
        }
    };
    let backup = if dry_run {
        None
    } else {
        match plan.apply() {
            Ok(result) => result.backup_path,
            Err(error) => {
                eprintln!("sidepulse-next: {error}");
                return ExitCode::FAILURE;
            }
        }
    };
    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "provider": plan.provider,
                "config_path": plan.config_path,
                "log_path": plan.log_path,
                "changed": plan.changed,
                "backup_path": backup,
                "dry_run": dry_run,
                "updated": if dry_run { Some(plan.updated) } else { None },
            }))
            .expect("hook config result serializes")
        );
    } else {
        println!(
            "{} {}: {}",
            provider,
            if action == Action::Install {
                "install"
            } else {
                "uninstall"
            },
            if dry_run {
                "planned"
            } else if plan.changed {
                "changed"
            } else {
                "unchanged"
            }
        );
        if dry_run && plan.changed {
            print!("{}", plan.updated);
        }
    }
    ExitCode::SUCCESS
}
