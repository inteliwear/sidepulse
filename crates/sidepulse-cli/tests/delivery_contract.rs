//! Real CLI and IPC, using only a temporary LED target and local HTTP server.
use interprocess::local_socket::prelude::*;
use serde_json::{Value, json};
use std::{
    fs,
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{SystemTime, UNIX_EPOCH},
};

#[test]
fn manual_delivery_and_saved_phone_links_are_owned_by_the_service() {
    #[cfg(unix)]
    let dir = tempfile::tempdir_in("/tmp").unwrap();
    #[cfg(windows)]
    let dir = tempfile::tempdir().unwrap();
    let id = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    #[cfg(unix)]
    let endpoint = dir
        .path()
        .join(format!("d-{id}.sock"))
        .to_string_lossy()
        .into_owned();
    #[cfg(windows)]
    let endpoint = format!("sidepulse-delivery-{id}");
    let links = dir.path().join("links.json");
    let settings = dir.path().join("settings.json");
    fs::write(&links, r#"{"version":1,"unknown":7,"ios":[]}"#).unwrap();
    fs::write(&settings, r#"{"unknown":8,"led_display":"agent"}"#).unwrap();
    let device = dir.path().join("TEST");
    fs::create_dir(&device).unwrap();
    let target = device.join(sidepulse_device::DEFAULT_FILE_NAME);
    fs::write(&target, "off").unwrap();
    let service = sidepulse_daemon::Service::new();
    service.configure_phone_links(&links).unwrap();
    service.configure_settings(&settings).unwrap();
    service.configure_device(&target, 173).unwrap();
    let listener = sidepulse_ipc::bind(&endpoint).unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let stop_server = stop.clone();
    let server = std::thread::spawn(move || {
        loop {
            let stream = listener.accept().unwrap();
            if stop_server.load(Ordering::Acquire) {
                break;
            }
            service.serve_connection(stream).unwrap();
        }
    });
    let run = |command: &str, args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_sidepulse-next"))
            .arg(command)
            .args(args)
            .args(["--endpoint", &endpoint])
            .output()
            .unwrap()
    };
    let phone = run(
        "phone-link",
        &[
            "register",
            "--token",
            &"ab".repeat(32),
            "--name",
            "Test phone",
            "--server",
            "http://127.0.0.1:7777",
        ],
    );
    assert!(
        phone.status.success(),
        "{}",
        String::from_utf8_lossy(&phone.stderr)
    );
    let summary: Value = serde_json::from_slice(&phone.stdout).unwrap();
    assert_eq!(summary["links"][0]["name"], "Test phone");
    assert!(summary["links"][0].get("token").is_none());
    let dry = run("push", &["#00E5FF", "--dry-run"]);
    assert!(
        dry.status.success(),
        "{}",
        String::from_utf8_lossy(&dry.stderr)
    );
    let dry: Value = serde_json::from_slice(&dry.stdout).unwrap();
    assert_eq!(dry[0]["destination"]["kind"], "phone");
    assert_eq!(dry[0]["status"], "planned");
    assert_eq!(fs::read_to_string(&target).unwrap(), "off");
    let written = run("write", &["#00E5FF", "--device", device.to_str().unwrap()]);
    assert!(
        written.status.success(),
        "{}",
        String::from_utf8_lossy(&written.stderr)
    );
    assert_eq!(fs::read_to_string(&target).unwrap(), "#00E5FF");
    let saved: Value = serde_json::from_slice(&fs::read(&settings).unwrap()).unwrap();
    assert_eq!(saved["unknown"], 8);
    assert_eq!(saved["devices"][0]["led_display"], "custom");
    let invalid = run(
        "write",
        &[
            "#00E5FF",
            "--device",
            device.to_str().unwrap(),
            "--title",
            "Hello",
        ],
    );
    assert!(!invalid.status.success());
    assert_eq!(fs::read_to_string(&target).unwrap(), "#00E5FF");
    let custom = run(
        "write",
        &[
            "off",
            "--device",
            device.to_str().unwrap(),
            "--file-name",
            "TEST.LED",
        ],
    );
    assert!(
        custom.status.success(),
        "{}",
        String::from_utf8_lossy(&custom.stderr)
    );
    assert_eq!(fs::read_to_string(device.join("TEST.LED")).unwrap(), "off");
    assert_eq!(fs::read_to_string(&target).unwrap(), "#00E5FF");
    let removed = run("phone-link", &["remove", "abababababab"]);
    assert!(removed.status.success());
    let saved: Value = serde_json::from_slice(&fs::read(links).unwrap()).unwrap();
    assert_eq!(saved["unknown"], 7);
    assert_eq!(saved["ios"], json!([]));
    stop.store(true, Ordering::Release);
    drop(sidepulse_ipc::connect(&endpoint, std::time::Duration::from_secs(2)).unwrap());
    server.join().unwrap();
}
