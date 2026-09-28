use interprocess::local_socket::prelude::*;
use serde_json::Value;
use std::fs;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

#[test]
fn profiles_and_named_assets_round_trip_through_cli_and_ipc() {
    #[cfg(unix)]
    let directory = tempfile::tempdir_in("/tmp").unwrap();
    #[cfg(windows)]
    let directory = tempfile::tempdir().unwrap();
    let id = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    #[cfg(unix)]
    let endpoint = directory
        .path()
        .join(format!("a-{id}.sock"))
        .to_string_lossy()
        .into_owned();
    #[cfg(windows)]
    let endpoint = format!("sidepulse-animation-{id}");
    let path = directory.path().join("settings.json");
    fs::write(&path, r#"{"other":7}"#).unwrap();
    let service = sidepulse_daemon::Service::new();
    service.configure_settings(&path).unwrap();
    let listener = sidepulse_ipc::bind(&endpoint).unwrap();
    let server = std::thread::spawn(move || {
        for _ in 0..12 {
            service
                .serve_connection(listener.accept().unwrap())
                .unwrap();
        }
    });
    let run = |command: &str, args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
            .args([command, &endpoint])
            .args(args)
            .output()
            .unwrap()
    };
    let success = |command: &str, args: &[&str]| {
        let output = run(command, args);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice::<Value>(&output.stdout).unwrap()
    };
    let initial = success("animation-profile", &["list"]);
    assert_eq!(initial["matching_profile"], "profile:cyan");
    success("animation-profile", &["apply", "profile:ember"]);
    success("animation-profile", &["save", "My pattern"]);
    let exported = success("animation-profile", &["export", "profile:my-pattern"]);
    assert_eq!(exported["animations"]["working"], "ember-tide");
    assert_eq!(exported["animations"]["lid_open"], "ember-lid-open");
    let program = directory.path().join("custom.LED");
    fs::write(&program, "#FF0080").unwrap();
    success(
        "animation-asset",
        &["save", "My light", program.to_str().unwrap()],
    );
    success("service-animation", &["working", "custom:my-light"]);
    success("animation-profile", &["save", "Custom pattern"]);
    let mut exported = success("animation-profile", &["export", "profile:custom-pattern"]);
    assert_eq!(
        exported["custom_animations"]["custom:my-light"]["program"],
        "#FF0080"
    );
    let original = fs::read(&path).unwrap();
    let count = fs::read_dir(directory.path().join("animations"))
        .unwrap()
        .count();
    let incoming = directory.path().join("incoming.json");
    exported["custom_animations"]["custom:my-light"]["program"] =
        Value::String("this is invalid".into());
    fs::write(&incoming, exported.to_string()).unwrap();
    assert!(
        !run("animation-profile", &["import", incoming.to_str().unwrap()])
            .status
            .success()
    );
    assert_eq!(fs::read(&path).unwrap(), original);
    assert_eq!(
        fs::read_dir(directory.path().join("animations"))
            .unwrap()
            .count(),
        count
    );
    exported["custom_animations"]["custom:my-light"]["program"] = Value::String("#00FF00".into());
    fs::write(&incoming, exported.to_string()).unwrap();
    success("animation-profile", &["import", incoming.to_str().unwrap()]);
    let library = success("animation-profile", &["list"]);
    assert_eq!(library["current"]["working"], "custom:my-light-2");
    assert_eq!(library["current"]["tool_running"], "custom:my-light-2");
    assert_eq!(
        library["custom_animations"]["custom:my-light"]["program"],
        "#FF0080"
    );
    assert_eq!(
        library["custom_animations"]["custom:my-light-2"]["program"],
        "#00FF00"
    );
    assert!(
        !run("animation-profile", &["delete", "profile:cyan"])
            .status
            .success()
    );
    server.join().unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&fs::read(path).unwrap()).unwrap()["other"],
        7
    );
}
