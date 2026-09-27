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
    let mut hook = None;
    let mut home = None;
    let mut log_dir = None;
    let mut dry_run = false;
    let mut json_output = false;
    let mut args = args;
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--provider" | "--config" | "--log" | "--hook" | "--home" | "--log-dir" => {
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
                    _ => unreachable!(),
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
    let log_dir = log_dir.unwrap_or_else(|| default_log_dir(&home, !explicit_home));
    let hook = match hook {
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
        return run_all_providers(action, &home, &log_dir, &hook, dry_run, json_output);
    }
    let config = config.unwrap_or_else(|| home.join(provider_config_path(&provider)));
    let log = log.unwrap_or_else(|| log_dir.join(format!("{provider}.jsonl")));
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
            } else if plan.changed {
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
        .unwrap_or_else(|| home.join(".local/state"));
    state_root.join("sidepulse/agent-monitor")
}

fn provider_config_path(provider: &str) -> &'static str {
    match provider {
        "codex" => ".codex/config.toml",
        "claude" => ".claude/settings.json",
        "grok" => ".grok/hooks/sidepulse.json",
        "cursor" => ".cursor/hooks.json",
        "junie" => ".junie/config.json",
        _ => unreachable!("provider validated before use"),
    }
}

fn run_all_providers(
    action: Action,
    home: &std::path::Path,
    log_dir: &std::path::Path,
    hook: &std::path::Path,
    dry_run: bool,
    json_output: bool,
) -> ExitCode {
    let providers = ["codex", "claude", "grok", "cursor", "junie"];
    let plans: Result<Vec<HookPlan>, _> = providers
        .iter()
        .map(|provider| {
            let config = home.join(provider_config_path(provider));
            let log = log_dir.join(format!("{provider}.jsonl"));
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
    for legacy_name in ["sidepulse-agent-monitor.json", "sidepulse-cli.json"] {
        let legacy = home.join(".grok/hooks").join(legacy_name);
        if legacy.exists() {
            let log = log_dir.join("grok.jsonl");
            let plan = match plan_json_hooks("grok", &legacy, &log, hook, Action::Uninstall) {
                Ok(mut plan) => {
                    plan.provider = format!("grok legacy {legacy_name}");
                    plan
                }
                Err(error) => {
                    eprintln!("sidepulse-next: {}: {error}", legacy.display());
                    return ExitCode::FAILURE;
                }
            };
            plans.push(plan);
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
