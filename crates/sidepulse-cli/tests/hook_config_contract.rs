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
