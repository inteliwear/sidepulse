use std::fs;
use std::process::Command;

use serde_json::Value;

#[test]
fn stage_cli_plans_then_creates_one_isolated_bundle() {
    let directory = tempfile::tempdir().unwrap();
    let source = directory.path().join("binaries");
    let stage = directory.path().join("preview");
    fs::create_dir(&source).unwrap();
    for name in [
        "sidepulse-next",
        "sidepulse-next-hook",
        "sidepulse-next-service",
        "sidepulse-next-tray",
    ] {
        fs::write(
            source.join(format!("{name}{}", std::env::consts::EXE_SUFFIX)),
            b"binary",
        )
        .unwrap();
    }
    let command = |dry_run: bool| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_sidepulse-next-stage"));
        command
            .arg("--source-dir")
            .arg(&source)
            .arg("--stage-dir")
            .arg(&stage);
        if dry_run {
            command.arg("--dry-run");
        }
        command.output().unwrap()
    };
    let planned = command(true);
    assert!(
        planned.status.success(),
        "{}",
        String::from_utf8_lossy(&planned.stderr)
    );
    let preview: Value = serde_json::from_slice(&planned.stdout).unwrap();
    assert_eq!(preview["enabled"], false);
    assert!(!stage.exists());
    let staged = command(false);
    assert!(
        staged.status.success(),
        "{}",
        String::from_utf8_lossy(&staged.stderr)
    );
    let actual: Value = serde_json::from_slice(&staged.stdout).unwrap();
    assert_eq!(actual, preview);
    assert!(stage.join("manifest.json").is_file());
    let repeated = command(false);
    assert!(!repeated.status.success());
    assert_eq!(
        fs::read(
            stage
                .join("bin")
                .join(format!("sidepulse-next{}", std::env::consts::EXE_SUFFIX))
        )
        .unwrap(),
        b"binary"
    );
}
