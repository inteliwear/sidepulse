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
