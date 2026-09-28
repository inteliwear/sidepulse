use serde_json::Value;
use std::{fs, process::Command};
#[test]
fn setup_and_startup_plans_stay_inside_an_explicit_preview() {
    let root = tempfile::tempdir().unwrap();
    let source = root.path().join("source");
    fs::create_dir(&source).unwrap();
    let stage = root.path().join("preview");
    let startup = root.path().join("startup");
    for name in [
        "sidepulse-next",
        "sidepulse-next-hook",
        "sidepulse-next-service",
        "sidepulse-next-tray",
        "sidepulse-next-stage",
        "sidepulse-next-settings",
        "sidepulse-next-virtual",
        "sidepulse-next-sd-guard",
        "sidepulse-next-reply",
    ] {
        fs::write(
            source.join(format!("{name}{}", std::env::consts::EXE_SUFFIX)),
            b"binary",
        )
        .unwrap();
    }
    let setup = |dry_run| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"));
        command
            .arg("setup")
            .arg("--source-dir")
            .arg(&source)
            .arg("--stage-dir")
            .arg(&stage);
        if dry_run {
            command.arg("--dry-run");
        }
        command.output().unwrap()
    };
    let preview = setup(true);
    assert!(
        preview.status.success(),
        "{}",
        String::from_utf8_lossy(&preview.stderr)
    );
    assert!(!stage.exists());
    let manifest: Value = serde_json::from_slice(&preview.stdout).unwrap();
    assert_eq!(manifest["enabled"], false);
    assert_eq!(manifest["binaries"].as_array().unwrap().len(), 9);
    let installed = setup(false);
    assert!(installed.status.success());
    for job in ["service", "status-bar"] {
        let output = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
            .args([job, "install"])
            .arg("--stage-dir")
            .arg(&stage)
            .arg("--startup-dir")
            .arg(&startup)
            .args([
                "--user",
                if cfg!(target_os = "macos") {
                    "501"
                } else {
                    "S-1-5-21-123"
                },
                "--no-start",
                "--dry-run",
            ])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let plan: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(plan["start"], false);
        assert!(Path::new(plan["path"].as_str().unwrap()).starts_with(&startup));
        assert!(!startup.exists());
    }
    let run = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
        .args(["service", "run"])
        .arg("--stage-dir")
        .arg(&stage)
        .arg("--dry-run")
        .output()
        .unwrap();
    assert!(run.status.success());
    let run: Value = serde_json::from_slice(&run.stdout).unwrap();
    assert_eq!(run["command"], manifest["service_command"]);
    let mut edited: Value =
        serde_json::from_slice(&fs::read(stage.join("manifest.json")).unwrap()).unwrap();
    edited["service_command"][0] = Value::String("unrelated".into());
    fs::write(
        stage.join("manifest.json"),
        serde_json::to_vec(&edited).unwrap(),
    )
    .unwrap();
    let rejected = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
        .args(["service", "run"])
        .arg("--stage-dir")
        .arg(&stage)
        .arg("--dry-run")
        .output()
        .unwrap();
    assert!(!rejected.status.success());
}
use std::path::Path;
