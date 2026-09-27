use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use serde_json::json;
use sidepulse_sources::detect_provider_configs;

pub fn run_doctor(mut args: impl Iterator<Item = String>) -> ExitCode {
    let json_output = match (args.next().as_deref(), args.next()) {
        (None, None) => false,
        (Some("--json"), None) => true,
        _ => {
            eprintln!("usage: sidepulse-next agent-monitor doctor [--json]");
            return ExitCode::from(2);
        }
    };
    let home = env::var_os("HOME")
        .or_else(|| env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let reports = detect_provider_configs(&home);
    if json_output {
        let providers: Vec<_> = reports
            .iter()
            .map(|report| {
                json!({
                    "provider": report.provider,
                    "config_path": report.config_path,
                    "exists": report.exists,
                    "hooks_enabled": report.hooks_enabled,
                    "hook_events": report.hook_events,
                    "log_paths": report.log_paths,
                })
            })
            .collect();
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({"providers": providers})).unwrap()
        );
    } else {
        for report in reports {
            println!("{}:", report.provider);
            println!(
                "  config: {} ({})",
                report.config_path.display(),
                if report.exists { "found" } else { "missing" }
            );
            println!("  hooks enabled: {}", report.hooks_enabled);
            println!(
                "  events: {}",
                if report.hook_events.is_empty() {
                    "-".into()
                } else {
                    report.hook_events.join(", ")
                }
            );
            println!(
                "  logs: {}",
                if report.log_paths.is_empty() {
                    "-".into()
                } else {
                    report
                        .log_paths
                        .iter()
                        .map(|path| path.display().to_string())
                        .collect::<Vec<_>>()
                        .join(", ")
                }
            );
        }
    }
    ExitCode::SUCCESS
}
