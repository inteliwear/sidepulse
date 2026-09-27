use std::fs;
use std::process::Command;

use serde_json::Value;

#[test]
fn battery_status_reads_saved_baseline_and_accepts_explicit_override() {
    let directory = tempfile::tempdir().unwrap();
    let config = directory.path().join("sidepulse/agent-monitor");
    fs::create_dir_all(&config).unwrap();
    let settings = config.join("settings.json");
    let original = br#"{"battery_monitoring":{"full_charge_watts":140.0},"other":true}"#;
    fs::write(&settings, original).unwrap();
    let run = |extra: &[&str]| {
        let output = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
            .args(["battery", "status", "--json"])
            .args(extra)
            .env("HOME", directory.path())
            .env("USERPROFILE", directory.path())
            .env("XDG_CONFIG_HOME", directory.path())
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice::<Value>(&output.stdout).unwrap()
    };
    let saved = run(&[]);
    assert_eq!(saved["full_charge_watts"], 140.0);
    assert!(saved["battery_present"].is_boolean());
    assert!(saved["pd_profiles"].is_array());
    assert!(saved["adapter_power"].is_number());
    assert_eq!(run(&["--full-watts", "70"])["full_charge_watts"], 70.0);
    assert_eq!(fs::read(&settings).unwrap(), original);
}

#[test]
fn battery_configuration_is_saved_by_the_service_and_preserves_other_settings() {
    use interprocess::local_socket::prelude::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[cfg(unix)]
    let directory = tempfile::tempdir_in("/tmp").unwrap();
    #[cfg(windows)]
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("settings.json");
    fs::write(
        &path,
        r#"{"battery_monitoring":{"custom":"keep"},"unknown":9}"#,
    )
    .unwrap();
    let id = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    #[cfg(unix)]
    let endpoint = directory
        .path()
        .join(format!("b-{id}.sock"))
        .to_string_lossy()
        .into_owned();
    #[cfg(windows)]
    let endpoint = format!("sidepulse-battery-{id}");
    let listener = sidepulse_ipc::bind(&endpoint).unwrap();
    let service = sidepulse_daemon::Service::new();
    service.configure_settings(&path).unwrap();
    let server = std::thread::spawn(move || {
        for _ in 0..2 {
            service
                .serve_connection(listener.accept().unwrap())
                .unwrap();
        }
    });
    let run = |flags: &[&str]| {
        let output = Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
            .args(["battery", "configure", "--endpoint", &endpoint])
            .args(flags)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let text = String::from_utf8(output.stdout).unwrap();
        assert!(text.contains("power-change preview:"));
        serde_json::from_slice::<Value>(&fs::read(&path).unwrap()).unwrap()
    };
    let saved = run(&[
        "--display",
        "custom",
        "--full-watts",
        "140",
        "--show-on-power-change",
        "no",
        "--power-change-preview-seconds",
        "3",
    ]);
    assert_eq!(saved["unknown"], 9);
    assert_eq!(saved["battery_monitoring"]["custom"], "keep");
    assert_eq!(saved["battery_monitoring"]["full_charge_watts"], 140.0);
    assert_eq!(saved["battery_monitoring"]["show_on_power_change"], false);
    assert_eq!(
        saved["battery_monitoring"]["power_change_preview_seconds"],
        3.0
    );
    assert_eq!(saved["led_display"], "custom");
    let saved = run(&["--full-watts", "auto"]);
    assert!(saved["battery_monitoring"]["full_charge_watts"].is_null());
    server.join().unwrap();
}
