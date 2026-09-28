use serde_json::{Value, json};
use std::{fs, process::Command};

#[test]
fn cli_and_service_share_hook_setup_and_atomic_diagnostic_exports() {
    use interprocess::local_socket::prelude::*;
    #[cfg(unix)]
    let temp = tempfile::tempdir_in("/tmp").unwrap();
    #[cfg(windows)]
    let temp = tempfile::tempdir().unwrap();
    let id = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    #[cfg(unix)]
    let endpoint = temp
        .path()
        .join(format!("s-{id}.sock"))
        .to_string_lossy()
        .into_owned();
    #[cfg(windows)]
    let endpoint = format!("sidepulse-setup-{id}");
    let home = temp.path().join("home");
    fs::create_dir_all(home.join(".claude")).unwrap();
    let config = home.join(".claude/settings.json");
    fs::write(&config, br#"{"unrelated":42}"#).unwrap();
    let log = temp.path().join("state/claude.jsonl");
    fs::create_dir_all(log.parent().unwrap()).unwrap();
    let audit = log.with_file_name("event-status.jsonl");
    fs::write(
        &audit,
        json!({"provider":"claude","message":"quoted \"雪\""}).to_string(),
    )
    .unwrap();
    let service = sidepulse_daemon::Service::new();
    service
        .configure_hook_setup(
            &home,
            &[("claude".into(), log.clone())],
            std::path::Path::new(env!("CARGO_BIN_EXE_sidepulse-next-hook")),
            None,
        )
        .unwrap();
    service
        .configure_diagnostics(&audit, None, None, &temp.path().join("exports"))
        .unwrap();
    let listener = sidepulse_ipc::bind(&endpoint).unwrap();
    let server = std::thread::spawn(move || {
        for _ in 0..9 {
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
        let result = run(command, args);
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        serde_json::from_slice::<Value>(&result.stdout).unwrap()
    };
    assert!(
        success("setup-status", &[])["status"]["configured"]
            .as_bool()
            .unwrap()
    );
    assert_eq!(
        success("configure-hooks", &["claude", "install", "--dry-run"])["dry_run"],
        true
    );
    assert_eq!(fs::read_to_string(&config).unwrap(), "{\"unrelated\":42}");
    assert!(success("configure-hooks", &["claude", "install"])["backup_path"].is_string());
    let status = success("setup-status", &[]);
    assert_eq!(status["status"]["providers"][1]["native"], true);
    assert_eq!(
        status["status"]["providers"][1]["log_path"],
        log.to_string_lossy().as_ref()
    );
    assert!(
        success("diagnostics", &[])["status"]["audit_bytes"]
            .as_u64()
            .unwrap()
            > 0
    );
    for format in ["csv", "html"] {
        let result = success("diagnostics-export", &[format]);
        assert_eq!(result["events"], 1);
        assert!(
            fs::read_to_string(result["path"].as_str().unwrap())
                .unwrap()
                .contains("claude")
        );
    }
    success("configure-hooks", &["claude", "remove"]);
    assert_eq!(
        serde_json::from_slice::<Value>(&fs::read(&config).unwrap()).unwrap(),
        json!({"unrelated":42})
    );
    let invalid = run("configure-hooks", &["../escape", "install"]);
    assert!(!invalid.status.success());
    server.join().unwrap();
}
