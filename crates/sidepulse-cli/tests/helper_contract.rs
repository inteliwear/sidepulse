use serde_json::Value;
use std::{fs, process::Command};
#[test]
fn explicit_helper_dry_runs_preserve_files_and_reject_other_sudoers_rules() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("helper");
    let binary = env!("CARGO_BIN_EXE_sidepulse-next");
    let run = |operation: &str| {
        Command::new(binary)
            .args([
                "status-bar",
                operation,
                "--dry-run",
                "--user",
                "test-user",
                "--path",
            ])
            .arg(&path)
            .output()
            .unwrap()
    };
    let result = run("install-sleep-helper");
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let plan: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(plan["changed"], true);
    assert!(
        plan["rule"]
            .as_str()
            .unwrap()
            .contains("/usr/bin/pmset -a disablesleep 0")
    );
    assert!(!path.exists());
    fs::write(
        &path,
        sidepulse_helpers::sleep_helper::rule_for_user("test-user").unwrap(),
    )
    .unwrap();
    let result = run("uninstall-sleep-helper");
    assert!(result.status.success());
    assert!(path.exists());
    fs::write(&path, "test-user ALL=(ALL) NOPASSWD: ALL\n").unwrap();
    assert!(!run("install-sleep-helper").status.success());
    assert!(!run("uninstall-sleep-helper").status.success());
    assert!(fs::read_to_string(&path).unwrap().contains("NOPASSWD: ALL"));
    let status = Command::new(binary)
        .args(["status-bar", "sleep-helper-status", "--path"])
        .arg(&path)
        .output()
        .unwrap();
    assert!(status.status.success());
    assert_eq!(
        serde_json::from_slice::<Value>(&status.stdout).unwrap()["installed"],
        true
    );
}
