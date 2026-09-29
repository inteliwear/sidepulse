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

#[test]
fn explicit_legacy_import_is_reviewable_and_loads_custom_assets_without_source_changes() {
    let root = tempfile::tempdir().unwrap();
    let source = root.path().join("source");
    let stage = root.path().join("preview");
    let config = root.path().join("legacy config");
    let logs = root.path().join("legacy logs");
    fs::create_dir(&source).unwrap();
    fs::create_dir_all(config.join("animations")).unwrap();
    fs::create_dir(&logs).unwrap();
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
    let settings = br#"{"unknown":{"preserved":true},"agent_animations":{"working":{"style":"custom:sample"}},"custom_agent_animations":{"custom:sample":{"name":"Saved sample","file":"sample.LED"}},"agent_animation_profiles":{"profile:saved":{"name":"Saved profile","animations":{"working":"custom:sample"}}}}"#;
    let links = br#"{"version":1,"ios":[],"unknown":7}"#;
    fs::write(config.join("settings.json"), settings).unwrap();
    fs::write(config.join("links.json"), links).unwrap();
    fs::write(config.join("animations/sample.LED"), b"#00FF80").unwrap();
    fs::write(config.join("relay.json"), br#"{"enabled":true}"#).unwrap();
    fs::write(logs.join("junie.jsonl"), b"captured audit\n").unwrap();
    let setup = |dry_run| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"));
        command
            .args(["setup", "--source-dir"])
            .arg(&source)
            .arg("--stage-dir")
            .arg(&stage)
            .arg("--import-config")
            .arg(&config)
            .arg("--import-logs")
            .arg(&logs);
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
    let plan = setup(true);
    assert!(!stage.exists());
    assert_eq!(plan["imported_files"].as_array().unwrap().len(), 4);
    setup(false);
    assert_eq!(fs::read(stage.join("settings.json")).unwrap(), settings);
    assert_eq!(fs::read(stage.join("links.json")).unwrap(), links);
    assert_eq!(fs::read(config.join("settings.json")).unwrap(), settings);
    assert_eq!(fs::read(config.join("links.json")).unwrap(), links);
    assert_eq!(
        fs::read(config.join("animations/sample.LED")).unwrap(),
        b"#00FF80"
    );
    assert_eq!(
        fs::read(stage.join("state/junie.jsonl")).unwrap(),
        b"captured audit\n"
    );
    let relay: Value =
        serde_json::from_slice(&fs::read(stage.join("relay.json")).unwrap()).unwrap();
    assert_eq!(relay, serde_json::json!({"version":1}));
    let service = sidepulse_daemon::Service::new();
    service
        .configure_settings(&stage.join("settings.json"))
        .unwrap();
    let library = service.animation_library().unwrap();
    assert_eq!(
        library.custom_animations["custom:sample"].program,
        "#00FF80"
    );
    assert_eq!(library.current["working"], "custom:sample");
    assert_eq!(library.profiles["profile:saved"].name, "Saved profile");
    assert_eq!(fs::read(config.join("settings.json")).unwrap(), settings);
}
