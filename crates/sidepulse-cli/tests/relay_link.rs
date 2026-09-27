use std::process::Command;

use sidepulse_relay::{DEFAULT_BRIDGE_SERVER, load_config};

#[test]
fn link_command_creates_receiver_and_configures_source_without_installing() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("relay.json");
    let binary = env!("CARGO_BIN_EXE_sidepulse-next");
    let first = Command::new(binary)
        .args(["link", "--config"])
        .arg(&path)
        .output()
        .unwrap();
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    let receiver = load_config(&path, "Host").unwrap().receiver_channel;
    assert_eq!(receiver.len(), 22);
    let second = Command::new(binary)
        .args(["link", "--config"])
        .arg(&path)
        .output()
        .unwrap();
    assert!(second.status.success());
    assert_eq!(
        load_config(&path, "Host").unwrap().receiver_channel,
        receiver
    );
    let source = Command::new(binary)
        .args(["link", &receiver, "--config"])
        .arg(&path)
        .output()
        .unwrap();
    assert!(source.status.success());
    let config = load_config(&path, "Host").unwrap();
    assert_eq!(config.outbound_channel, receiver);
    assert_eq!(config.server, DEFAULT_BRIDGE_SERVER);
}
