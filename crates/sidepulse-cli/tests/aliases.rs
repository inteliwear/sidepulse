use std::{fs, process::Command};
#[test]
fn existing_entry_point_names_route_to_native_commands() {
    let root = tempfile::tempdir().unwrap();
    for (name, args) in [
        ("sidepulse", vec!["--version"]),
        ("agent-monitor", vec!["version"]),
        ("agent-status-bar", vec!["--help"]),
    ] {
        let alias = root
            .path()
            .join(format!("{name}{}", std::env::consts::EXE_SUFFIX));
        fs::copy(env!("CARGO_BIN_EXE_sidepulse-next"), &alias).unwrap();
        let output = Command::new(&alias).args(args).output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(!output.stdout.is_empty());
    }
}
