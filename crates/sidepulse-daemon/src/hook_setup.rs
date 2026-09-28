//! Provider setup uses the same backed-up plans as the CLI. No UI owns files.
use sidepulse_core::{HookSetupStatus, ProviderHookStatus, ServerPayload};
use sidepulse_hook_config::{
    Action, HookPlan, plan_codex_hooks, plan_json_hooks, provider_config_path,
};
use std::{
    collections::BTreeMap,
    fs, io,
    path::{Path, PathBuf},
};

pub(super) struct HookSetup {
    home: PathBuf,
    executable: PathBuf,
    logs: BTreeMap<String, PathBuf>,
    stage: Option<PathBuf>,
    startup_directory: Option<PathBuf>,
}

impl HookSetup {
    fn plan(&self, provider: &str, action: Action) -> io::Result<HookPlan> {
        let relative = provider_config_path(provider)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "unknown hook provider"))?;
        let config = self.home.join(relative);
        if fs::metadata(&config)
            .is_ok_and(|metadata| !metadata.is_file() || metadata.len() > 1024 * 1024)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "provider configuration must be a file smaller than 1 MiB",
            ));
        }
        let log = self
            .logs
            .get(provider)
            .ok_or_else(|| io::Error::other("provider log is unavailable"))?;
        if provider == "codex" {
            plan_codex_hooks(&config, log, &self.executable, action)
        } else {
            plan_json_hooks(provider, &config, log, &self.executable, action)
        }
    }

    fn plans(&self, provider: &str, action: Action) -> io::Result<Vec<HookPlan>> {
        let mut plans = vec![self.plan(provider, action)?];
        if provider == "grok" {
            plans.extend(sidepulse_hook_config::plan_grok_legacy_removal(
                &self.home,
                &self.logs[provider],
                &self.executable,
            )?);
        }
        Ok(plans)
    }

    pub(super) fn cursor_source(&self) -> Option<(String, PathBuf)> {
        self.plan("cursor", Action::Uninstall)
            .ok()
            .filter(|plan| plan.changed)
            .and_then(|_| self.logs.get("cursor").cloned())
            .map(|path| ("cursor".into(), path))
    }

    fn status(&self) -> HookSetupStatus {
        let providers = ["codex", "claude", "grok", "cursor", "junie"]
            .into_iter()
            .map(|provider| {
                let result = self.plans(provider, Action::Install).and_then(|install| {
                    self.plans(provider, Action::Uninstall).map(|remove| {
                        (
                            remove.iter().any(|plan| plan.changed),
                            !install.iter().any(|plan| plan.changed),
                        )
                    })
                });
                let (installed, native, error) = match result {
                    Ok((installed, native)) => (installed, native, None),
                    Err(error) => (false, false, Some(error.to_string())),
                };
                ProviderHookStatus {
                    provider: provider.into(),
                    config_path: self
                        .home
                        .join(provider_config_path(provider).unwrap())
                        .to_string_lossy()
                        .into_owned(),
                    log_path: self.logs[provider].to_string_lossy().into_owned(),
                    installed,
                    native,
                    error,
                }
            })
            .collect();
        HookSetupStatus {
            configured: self.executable.is_file(),
            stage_dir: self
                .stage
                .as_ref()
                .map(|path| path.to_string_lossy().into_owned()),
            startup_directory: self
                .startup_directory
                .as_ref()
                .map(|path| path.to_string_lossy().into_owned()),
            home: self.home.to_string_lossy().into_owned(),
            providers,
        }
    }
}

impl super::Service {
    pub fn configure_hook_setup(
        &self,
        home: &Path,
        logs: &[(String, PathBuf)],
        executable: &Path,
        stage: Option<&Path>,
    ) -> io::Result<()> {
        let state_root = std::env::var_os("XDG_STATE_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".local/state"));
        let default = state_root.join("sidepulse/agent-monitor");
        let logs = ["codex", "claude", "grok", "cursor", "junie"]
            .into_iter()
            .map(|provider| {
                let path = logs
                    .iter()
                    .rev()
                    .find(|(name, _)| name == provider)
                    .map(|(_, path)| path.clone())
                    .unwrap_or_else(|| default.join(format!("{provider}.jsonl")));
                (provider.to_owned(), path)
            })
            .collect();
        *self.hook_setup.lock().map_err(super::poisoned)? = Some(HookSetup {
            home: home.into(),
            executable: executable.into(),
            logs,
            stage: stage.map(Path::to_path_buf),
            startup_directory: match std::env::consts::OS {
                "macos" => Some(home.join("Library/LaunchAgents")),
                "linux" => Some(
                    std::env::var_os("XDG_CONFIG_HOME")
                        .map(PathBuf::from)
                        .unwrap_or_else(|| home.join(".config"))
                        .join("systemd/user"),
                ),
                "windows" => std::env::var_os("LOCALAPPDATA")
                    .map(PathBuf::from)
                    .map(|path| path.join("SidePulse/Startup")),
                _ => None,
            },
        });
        Ok(())
    }

    pub fn hook_setup_status(&self) -> io::Result<HookSetupStatus> {
        Ok(self
            .hook_setup
            .lock()
            .map_err(super::poisoned)?
            .as_ref()
            .map_or_else(
                || HookSetupStatus {
                    configured: false,
                    stage_dir: None,
                    startup_directory: None,
                    home: String::new(),
                    providers: vec![],
                },
                HookSetup::status,
            ))
    }

    pub fn configure_provider_hooks(
        &self,
        provider: &str,
        install: bool,
        dry_run: bool,
    ) -> io::Result<ServerPayload> {
        let setup = self.hook_setup.lock().map_err(super::poisoned)?;
        let setup = setup
            .as_ref()
            .ok_or_else(|| io::Error::other("hook setup is unavailable in this service"))?;
        if install && !setup.executable.is_file() {
            return Err(io::Error::other("native hook executable is missing"));
        }
        let plans = setup.plans(
            provider,
            if install {
                Action::Install
            } else {
                Action::Uninstall
            },
        )?;
        let backup = if dry_run {
            None
        } else {
            let results = if provider == "grok" {
                sidepulse_hook_config::apply_plans_with_grok_backup_relocation(
                    &plans,
                    &setup.home.join(".grok/hooks"),
                )?
            } else {
                sidepulse_hook_config::apply_plans_atomically(&plans)?
            };
            results.into_iter().find_map(|result| result.backup_path)
        };
        Ok(ServerPayload::HooksConfigured {
            provider: provider.into(),
            install,
            changed: plans.iter().any(|plan| plan.changed),
            backup_path: backup.map(|path| path.to_string_lossy().into_owned()),
            trust_review_required: provider == "codex" && install,
            dry_run,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn setup_uses_explicit_logs_preserves_unrelated_hooks_and_detects_conflicts() {
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path().join("home");
        fs::create_dir_all(home.join(".claude")).unwrap();
        let config = home.join(".claude/settings.json");
        let original = json!({"unknown":42,"hooks":{"Stop":[{"hooks":[{"type":"command","command":"keep-me"}]}]}});
        fs::write(&config, serde_json::to_vec(&original).unwrap()).unwrap();
        let hook = temp.path().join("sidepulse-next-hook");
        fs::write(&hook, "fixture").unwrap();
        let log = temp.path().join("preview/state/claude.jsonl");
        let service = super::super::Service::new();
        service
            .configure_hook_setup(&home, &[("claude".into(), log.clone())], &hook, None)
            .unwrap();
        service
            .configure_provider_hooks("claude", true, true)
            .unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&fs::read(&config).unwrap()).unwrap(),
            original
        );
        let result = service
            .configure_provider_hooks("claude", true, false)
            .unwrap();
        assert!(matches!(
            result,
            ServerPayload::HooksConfigured {
                backup_path: Some(_),
                ..
            }
        ));
        let status = service.hook_setup_status().unwrap();
        let claude = status
            .providers
            .iter()
            .find(|entry| entry.provider == "claude")
            .unwrap();
        assert!(claude.installed && claude.native);
        assert_eq!(claude.log_path, log.to_string_lossy());
        let installed = fs::read_to_string(&config).unwrap();
        assert!(
            installed.contains("keep-me")
                && installed.contains(&hook.to_string_lossy().replace('\\', "\\\\"))
        );
        service
            .configure_provider_hooks("claude", false, false)
            .unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&fs::read(&config).unwrap()).unwrap(),
            original
        );
        assert!(
            service
                .configure_provider_hooks("../escape", true, false)
                .is_err()
        );
        fs::write(&config, "invalid JSON").unwrap();
        assert!(
            service
                .configure_provider_hooks("claude", true, false)
                .is_err()
        );
        assert_eq!(fs::read_to_string(&config).unwrap(), "invalid JSON");
    }
    #[test]
    fn grok_setup_removes_legacy_duplicate_handlers_and_relocates_backups() {
        let temp = tempfile::tempdir().unwrap();
        let hooks = temp.path().join(".grok/hooks");
        fs::create_dir_all(&hooks).unwrap();
        let hook = temp.path().join("sidepulse-next-hook");
        fs::write(&hook, "fixture").unwrap();
        let legacy = hooks.join("sidepulse-cli.json");
        let log = temp.path().join("grok.jsonl");
        let plan = plan_json_hooks("grok", &legacy, &log, &hook, Action::Install).unwrap();
        plan.apply().unwrap();
        let service = super::super::Service::new();
        service
            .configure_hook_setup(temp.path(), &[("grok".into(), log)], &hook, None)
            .unwrap();
        let status = service.hook_setup_status().unwrap();
        assert!(status.providers[2].installed && !status.providers[2].native);
        service
            .configure_provider_hooks("grok", true, false)
            .unwrap();
        assert!(service.hook_setup_status().unwrap().providers[2].native);
        assert!(
            !fs::read_to_string(&legacy)
                .unwrap()
                .contains("sidepulse-next-hook")
        );
        assert!(temp.path().join(".grok/sidepulse-hook-backups").is_dir());
        assert!(!fs::read_dir(&hooks).unwrap().any(|entry| {
            entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .contains(".bak.")
        }));
        service
            .configure_provider_hooks("grok", false, false)
            .unwrap();
        assert!(!service.hook_setup_status().unwrap().providers[2].installed);
    }
}
