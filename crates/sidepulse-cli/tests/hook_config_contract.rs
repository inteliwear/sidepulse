use std::fs;
use std::process::Command;

use serde_json::Value;

#[test]
fn explicit_paths_support_plan_apply_and_uninstall_without_touching_home() {
    let dir = std::env::temp_dir().join(format!(
        "sidepulse-hook-cli-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir(&dir).unwrap();
    let config = dir.join("settings.json");
    let log = dir.join("claude.jsonl");
    let hook = dir.join("sidepulse-next-hook");
    let run = |action: &str, dry_run: bool| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"));
        command
            .args(["agent-monitor", action, "--provider", "claude", "--config"])
            .arg(&config)
            .arg("--log")
            .arg(&log)
            .arg("--hook")
            .arg(&hook)
            .arg("--json")
            .env("HOME", &dir);
        if dry_run {
            command.arg("--dry-run");
        }
        let output = command.output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice::<Value>(&output.stdout).unwrap()
    };
    let planned = run("install", true);
    assert_eq!(planned["changed"], true);
    assert!(!config.exists());
    let installed = run("install", false);
    assert_eq!(installed["changed"], true);
    assert!(config.exists());
    assert_eq!(run("install", false)["changed"], false);
    assert_eq!(run("uninstall", false)["changed"], true);
    assert_eq!(run("uninstall", false)["changed"], false);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn explicit_home_batch_plans_and_installs_five_providers() {
    let directory = tempfile::tempdir().unwrap();
    let home = directory.path().join("home");
    let logs = directory.path().join("logs");
    fs::create_dir_all(home.join(".claude")).unwrap();
    fs::create_dir_all(home.join(".grok/hooks")).unwrap();
    fs::write(home.join(".claude/settings.json"), r#"{"theme":"dark"}"#).unwrap();
    let legacy = home.join(".grok/hooks/sidepulse-agent-monitor.json");
    fs::write(&legacy, r#"{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"user-hook"},{"type":"command","command":"sidepulse hook-log --log /tmp/old.jsonl"}]}]}}"#).unwrap();
    let hook = directory.path().join("sidepulse-next-hook");
    let run = |action: &str, dry_run: bool| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"));
        command
            .args(["agent-monitor", action, "--provider", "all", "--home"])
            .arg(&home)
            .arg("--log-dir")
            .arg(&logs)
            .arg("--hook")
            .arg(&hook)
            .arg("--json");
        if dry_run {
            command.arg("--dry-run");
        }
        let output = command.output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice::<Value>(&output.stdout).unwrap()
    };
    assert_eq!(
        run("install", true)["providers"].as_array().unwrap().len(),
        6
    );
    assert!(!home.join(".codex/config.toml").exists());
    assert!(
        run("install", false)["providers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|item| item["changed"] == true)
    );
    let legacy_document: Value = serde_json::from_slice(&fs::read(&legacy).unwrap()).unwrap();
    assert_eq!(
        legacy_document["hooks"]["Stop"][0]["hooks"][0]["command"],
        "user-hook"
    );
    assert_eq!(
        legacy_document["hooks"]["Stop"][0]["hooks"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert!(
        run("install", false)["providers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|item| item["changed"] == false)
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&fs::read(home.join(".claude/settings.json")).unwrap())
            .unwrap()["theme"],
        "dark"
    );
    assert!(
        run("uninstall", false)["providers"]
            .as_array()
            .unwrap()
            .iter()
            .take(5)
            .all(|item| item["changed"] == true)
    );
}
