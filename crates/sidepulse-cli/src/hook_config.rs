use std::collections::HashMap;
use std::env;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use serde_json::json;
use sidepulse_hook_config::{
    Action, HookPlan, apply_plans_with_grok_backup_relocation, plan_codex_hooks, plan_json_hooks,
};

pub fn run_hook_config(action: &str, args: impl Iterator<Item = String>) -> ExitCode {
    let action = match action {
        "install" => Action::Install,
        "uninstall" => Action::Uninstall,
        _ => return ExitCode::from(2),
    };
    let mut provider = None;
    let mut config = None;
    let mut log = None;
    let mut provider_logs = HashMap::new();
    let mut hook = None;
    let mut home = None;
    let mut log_dir = None;
    let mut dry_run = false;
    let mut json_output = false;
    let mut args = args;
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--provider" | "--config" | "--log" | "--hook" | "--home" | "--log-dir"
            | "--codex-log" | "--claude-log" | "--grok-log" | "--cursor-log" | "--junie-log" => {
                let Some(value) = args.next().filter(|value| !value.starts_with('-')) else {
                    eprintln!("sidepulse-next: {flag} needs a value");
                    return ExitCode::from(2);
                };
                match flag.as_str() {
                    "--provider" => provider = Some(value),
                    "--config" => config = Some(PathBuf::from(value)),
                    "--log" => log = Some(PathBuf::from(value)),
                    "--hook" => hook = Some(PathBuf::from(value)),
                    "--home" => home = Some(PathBuf::from(value)),
                    "--log-dir" => log_dir = Some(PathBuf::from(value)),
                    _ => {
                        let provider = flag.trim_start_matches("--").trim_end_matches("-log");
                        provider_logs.insert(provider.to_owned(), PathBuf::from(value));
                    }
                }
            }
            "--dry-run" => dry_run = true,
            "--json" => json_output = true,
            _ => {
                if !flag.starts_with('-') && provider.is_none() {
                    provider = Some(flag);
                    continue;
                }
                eprintln!("sidepulse-next: unknown hook config argument: {flag}");
                return ExitCode::from(2);
            }
        }
    }
    let provider = provider.unwrap_or_else(|| "all".to_owned());
    if !["all", "codex", "claude", "grok", "cursor", "junie"].contains(&provider.as_str()) {
        eprintln!("sidepulse-next: unsupported provider: {provider}");
        return ExitCode::from(2);
    }
    let explicit_home = home.is_some();
    let home = match home.or_else(|| {
        env::var_os("HOME")
            .or_else(|| env::var_os("USERPROFILE"))
            .map(PathBuf::from)
    }) {
        Some(home) => home,
        None => {
            eprintln!("sidepulse-next: cannot determine home directory; use --home DIR");
            return ExitCode::from(2);
        }
    };
    let log_dir = log_dir
        .map(|path| expand_user_path(&path, &home))
        .unwrap_or_else(|| default_log_dir(&home, !explicit_home));
    let config = config.map(|path| expand_user_path(&path, &home));
    let log = log.map(|path| expand_user_path(&path, &home));
    for path in provider_logs.values_mut() {
        *path = expand_user_path(path, &home);
    }
    let hook = match hook.map(|path| expand_user_path(&path, &home)) {
        Some(hook) => hook,
        None => match env::current_exe() {
            Ok(exe) => {
                let hook =
                    exe.with_file_name(format!("sidepulse-next-hook{}", env::consts::EXE_SUFFIX));
                if action == Action::Install && !hook.is_file() {
                    eprintln!(
                        "sidepulse-next: hook executable missing: {}",
                        hook.display()
                    );
                    return ExitCode::FAILURE;
                }
                hook
            }
            Err(error) => {
                eprintln!("sidepulse-next: cannot find hook executable: {error}");
                return ExitCode::FAILURE;
            }
        },
    };
    if provider == "all" {
        if config.is_some() || log.is_some() {
            eprintln!("sidepulse-next: --config and --log apply only to one provider");
            return ExitCode::from(2);
        }
        return run_all_providers(
            action,
            &home,
            &log_dir,
            &provider_logs,
            &hook,
            dry_run,
            json_output,
        );
    }
    let config = config.unwrap_or_else(|| home.join(provider_config_path(&provider)));
    let log = log
        .or_else(|| provider_logs.remove(&provider))
        .unwrap_or_else(|| log_dir.join(format!("{provider}.jsonl")));
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
    let mut plans = vec![plan];
    if provider == "grok" {
        match sidepulse_hook_config::plan_grok_legacy_removal(&home, &log, &hook) {
            Ok(legacy) => plans.extend(legacy),
            Err(error) => {
                eprintln!("sidepulse-next: {error}");
                return ExitCode::FAILURE;
            }
        }
    }
    let changed = plans.iter().any(|plan| plan.changed);
    let backup = if dry_run {
        None
    } else {
        let result = if provider == "grok" {
            apply_plans_with_grok_backup_relocation(&plans, &home.join(".grok/hooks"))
        } else {
            sidepulse_hook_config::apply_plans_atomically(&plans)
        };
        match result {
            Ok(results) => results.into_iter().find_map(|result| result.backup_path),
            Err(error) => {
                eprintln!("sidepulse-next: {error}");
                return ExitCode::FAILURE;
            }
        }
    };
    let plan = &plans[0];
    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "provider": plan.provider,
                "config_path": plan.config_path,
                "log_path": plan.log_path,
                "changed": changed,
                "backup_path": backup,
                "dry_run": dry_run,
                "updated": if dry_run { Some(&plan.updated) } else { None },
                "trust_review_required": provider == "codex" && action == Action::Install,
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
            } else if changed {
                "changed"
            } else {
                "unchanged"
            }
        );
        if dry_run && plan.changed {
            print!("{}", plan.updated);
        }
        if provider == "codex" && action == Action::Install && !dry_run {
            println!("Review and trust the new SidePulse hooks in Codex with /hooks.");
        }
    }
    ExitCode::SUCCESS
}

fn default_log_dir(home: &Path, use_xdg: bool) -> PathBuf {
    let state_root = use_xdg
        .then(|| env::var_os("XDG_STATE_HOME"))
        .flatten()
        .map(PathBuf::from)
        .map(|path| {
            if path == Path::new("~") {
                home.to_path_buf()
            } else if let Ok(suffix) = path.strip_prefix("~/") {
                home.join(suffix)
            } else {
                path
            }
        })
        .unwrap_or_else(|| home.join(".local").join("state"));
    state_root.join("sidepulse").join("agent-monitor")
}

fn expand_user_path(path: &Path, home: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    if text == "~" {
        home.to_path_buf()
    } else if let Some(relative) = text.strip_prefix("~/").or_else(|| text.strip_prefix("~\\")) {
        home.join(relative)
    } else {
        path.to_path_buf()
    }
}

fn provider_config_path(provider: &str) -> &'static Path {
    sidepulse_hook_config::provider_config_path(provider).expect("provider validated before use")
}

fn run_all_providers(
    action: Action,
    home: &std::path::Path,
    log_dir: &std::path::Path,
    provider_logs: &HashMap<String, PathBuf>,
    hook: &std::path::Path,
    dry_run: bool,
    json_output: bool,
) -> ExitCode {
    let providers = ["codex", "claude", "grok", "cursor", "junie"];
    let plans: Result<Vec<HookPlan>, _> = providers
        .iter()
        .map(|provider| {
            let config = home.join(provider_config_path(provider));
            let log = provider_logs
                .get(*provider)
                .cloned()
                .unwrap_or_else(|| log_dir.join(format!("{provider}.jsonl")));
            if *provider == "codex" {
                plan_codex_hooks(&config, &log, hook, action)
            } else {
                plan_json_hooks(provider, &config, &log, hook, action)
            }
        })
        .collect();
    let mut plans = match plans {
        Ok(plans) => plans,
        Err(error) => {
            eprintln!("sidepulse-next: {error}");
            return ExitCode::FAILURE;
        }
    };
    let grok_log = provider_logs
        .get("grok")
        .cloned()
        .unwrap_or_else(|| log_dir.join("grok.jsonl"));
    match sidepulse_hook_config::plan_grok_legacy_removal(home, &grok_log, hook) {
        Ok(legacy) => plans.extend(legacy),
        Err(error) => {
            eprintln!("sidepulse-next: {error}");
            return ExitCode::FAILURE;
        }
    }
    let backups = if dry_run {
        vec![None; plans.len()]
    } else {
        match apply_plans_with_grok_backup_relocation(&plans, &home.join(".grok/hooks")) {
            Ok(results) => results
                .into_iter()
                .map(|result| result.backup_path)
                .collect(),
            Err(error) => {
                eprintln!("sidepulse-next: {error}");
                return ExitCode::FAILURE;
            }
        }
    };
    if json_output {
        let results: Vec<_> = plans
            .iter()
            .zip(backups)
            .map(|(plan, backup)| {
                json!({
                    "provider": plan.provider,
                    "config_path": plan.config_path,
                    "log_path": plan.log_path,
                    "changed": plan.changed,
                    "backup_path": backup,
                    "dry_run": dry_run,
                    "updated": if dry_run { Some(&plan.updated) } else { None },
                    "trust_review_required": plan.provider == "codex" && action == Action::Install,
                })
            })
            .collect();
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({"providers": results})).unwrap()
        );
    } else {
        for plan in plans {
            println!(
                "{} {}: {}",
                plan.provider,
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
        }
        if action == Action::Install && !dry_run {
            println!("Review and trust the new SidePulse hooks in Codex with /hooks.");
        }
    }
    ExitCode::SUCCESS
}
