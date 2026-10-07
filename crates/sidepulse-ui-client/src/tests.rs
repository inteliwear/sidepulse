use super::*;
use interprocess::local_socket::prelude::*;
use std::io::BufReader;
use std::sync::atomic::{AtomicUsize, Ordering};

static NEXT_ENDPOINT: AtomicUsize = AtomicUsize::new(0);
fn endpoint(root: &std::path::Path) -> String {
    let id = NEXT_ENDPOINT.fetch_add(1, Ordering::Relaxed);
    #[cfg(unix)]
    {
        root.join(format!("client-{id}.sock"))
            .to_str()
            .unwrap()
            .into()
    }
    #[cfg(windows)]
    {
        let _ = root;
        format!("sidepulse-ui-client-{}-{id}", std::process::id())
    }
}
fn scratch() -> tempfile::TempDir {
    #[cfg(unix)]
    {
        tempfile::tempdir_in("/tmp").unwrap()
    }
    #[cfg(windows)]
    {
        tempfile::tempdir().unwrap()
    }
}

fn fixture(kind: RequestKind) -> ServerPayload {
    use sidepulse_core::{
        AnimationLibrary, DiagnosticsStatus, HookSetupStatus, Monitor, MonitoringPolicy,
        PowerControlStatus, RelaySettings,
    };
    match kind {
        RequestKind::Settings => ServerPayload::Settings {
            settings: serde_json::json!({"show_menu_bar_icon":false,"lid_open_animation":{"duration_seconds":3},"lid_closed_animation":{"duration_seconds":4}}),
            active_device: None,
            brightness: None,
            display_mode: None,
        },
        RequestKind::Snapshot => ServerPayload::Snapshot {
            state: {
                let mut monitor = Monitor::new(MonitoringPolicy::default());
                monitor.ingest(&sidepulse_core::parse_log_line("codex", &serde_json::json!({
                    "hook_event_name":"Stop", "session_id":"recent", "cwd":"/fixture/project",
                    "agent_origin":"Codex CLI", "logged_at":(chrono::Utc::now()-chrono::Duration::hours(2)).to_rfc3339(),
                }).to_string()).unwrap());
                monitor.snapshot(chrono::Utc::now())
            },
        },
        RequestKind::Devices => ServerPayload::Devices {
            devices: vec![],
            active_device: None,
        },
        RequestKind::Animations => ServerPayload::Animations {
            choices: vec![],
            states: vec![],
        },
        RequestKind::History => ServerPayload::History {
            points: vec![],
            timeframe_seconds: 7200,
            sampled: true,
        },
        RequestKind::AnimationLibrary => ServerPayload::AnimationLibrary {
            library: AnimationLibrary {
                profiles: Default::default(),
                custom_animations: Default::default(),
                current: Default::default(),
                matching_profile: None,
            },
        },
        RequestKind::RelaySettings => ServerPayload::RelaySettings {
            settings: RelaySettings {
                configured: false,
                server: "http://localhost/fixture".into(),
                machine_name: "Fixture".into(),
                receiver_code: String::new(),
                outbound_code: String::new(),
                last_received_at: None,
                last_sent_at: None,
                receive_error: None,
                send_error: None,
            },
        },
        RequestKind::PhoneLinks => ServerPayload::PhoneLinks {
            configured: false,
            output_enabled: false,
            links: vec![],
            pairing: None,
        },
        RequestKind::PowerControl => ServerPayload::PowerControl {
            status: PowerControlStatus::default(),
        },
        RequestKind::HookSetup => ServerPayload::HookSetup {
            status: HookSetupStatus {
                configured: false,
                stage_dir: None,
                startup_directory: None,
                home: "/fixture/home".into(),
                providers: vec![],
            },
        },
        RequestKind::Diagnostics => ServerPayload::Diagnostics {
            status: DiagnosticsStatus {
                settings_path: None,
                audit_path: None,
                audit_paths: vec![],
                audit_bytes: 42,
                history_path: None,
                export_directory: None,
            },
        },
        other => panic!("unexpected view request: {other:?}"),
    }
}

#[test]
fn shared_settings_view_loads_without_a_renderer() {
    let root = scratch();
    let endpoint = endpoint(root.path());
    let listener = sidepulse_ipc::bind(&endpoint).unwrap();
    let server = std::thread::spawn(move || {
        for _ in 0..11 {
            let mut stream = listener.accept().unwrap();
            let command: ClientRequest =
                sidepulse_ipc::read_message(&mut BufReader::new(&mut stream)).unwrap();
            command.validate().unwrap();
            sidepulse_ipc::write_message(
                &mut stream,
                &ServerMessage {
                    version: PROTOCOL_VERSION,
                    request_id: Some(command.request_id),
                    payload: fixture(command.kind),
                },
            )
            .unwrap();
        }
    });
    let state = fetch_state(&endpoint).unwrap_or_else(|error| panic!("{error}"));
    assert!(!state.settings.controls.visible);
    assert_eq!(state.history_timeframe, 7200);
    assert!(state.history_sampled);
    assert_eq!(state.lid_durations, [3.0, 4.0]);
    assert_eq!(state.diagnostics.audit_bytes, 42);
    assert!(state.devices.is_empty());
    assert_eq!(state.agents.len(), 1);
    assert_eq!(state.agents[0].agent_id, "codex:session:recent");
    assert!(state.agents[0].stale);
    assert_eq!(state.activity.stale_rows.len(), 1);
    assert!(!state.setup.configured && !state.phones_configured);
    server.join().unwrap();
}

#[test]
fn shared_client_rejects_mismatched_responses_and_preserves_service_errors() {
    for (version, id, payload, expected) in [
        (
            PROTOCOL_VERSION + 1,
            Some(1),
            ServerPayload::Ack,
            "invalid response",
        ),
        (
            PROTOCOL_VERSION,
            Some(2),
            ServerPayload::Ack,
            "invalid response",
        ),
        (
            PROTOCOL_VERSION,
            Some(1),
            ServerPayload::Error {
                code: "settings_conflict".into(),
                message: "Settings changed outside SidePulse".into(),
            },
            "Settings changed outside SidePulse",
        ),
    ] {
        let root = scratch();
        let endpoint = endpoint(root.path());
        let listener = sidepulse_ipc::bind(&endpoint).unwrap();
        let server = std::thread::spawn(move || {
            let mut stream = listener.accept().unwrap();
            let _: ClientRequest =
                sidepulse_ipc::read_message(&mut BufReader::new(&mut stream)).unwrap();
            sidepulse_ipc::write_message(
                &mut stream,
                &ServerMessage {
                    version,
                    request_id: id,
                    payload,
                },
            )
            .unwrap();
        });
        let error = fetch_state(&endpoint)
            .err()
            .expect("invalid reply must fail");
        assert!(error.contains(expected), "{error}");
        server.join().unwrap();
    }
}

#[test]
fn preview_command_is_not_delayed_by_a_settings_refresh() {
    let root = scratch();
    let endpoint = endpoint(root.path());
    let listener = sidepulse_ipc::bind(&endpoint).unwrap();
    let (refresh_started, refresh_seen) = mpsc::channel();
    let server = std::thread::spawn(move || {
        let mut refresh = listener.accept().unwrap();
        let request: ClientRequest =
            sidepulse_ipc::read_message(&mut BufReader::new(&mut refresh)).unwrap();
        assert!(matches!(request.kind, RequestKind::Settings));
        refresh_started.send(()).unwrap();
        let delayed = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(800));
            let _ = sidepulse_ipc::write_message(
                &mut refresh,
                &ServerMessage {
                    version: PROTOCOL_VERSION,
                    request_id: Some(request.request_id),
                    payload: fixture(RequestKind::Settings),
                },
            );
        });
        let mut preview = listener.accept().unwrap();
        let request: ClientRequest =
            sidepulse_ipc::read_message(&mut BufReader::new(&mut preview)).unwrap();
        assert!(matches!(request.kind, RequestKind::PreviewAnimation { .. }));
        sidepulse_ipc::write_message(
            &mut preview,
            &ServerMessage {
                version: PROTOCOL_VERSION,
                request_id: Some(request.request_id),
                payload: ServerPayload::Ack,
            },
        )
        .unwrap();
        delayed.join().unwrap();
    });
    let (commands, updates) = start_worker(endpoint);
    refresh_seen.recv_timeout(Duration::from_secs(1)).unwrap();
    let started = std::time::Instant::now();
    commands
        .send(WorkerCommand::Request(RequestKind::PreviewAnimation {
            state: "working".into(),
            program: None,
            seconds: 3,
        }))
        .unwrap();
    let update = updates.recv_timeout(Duration::from_millis(500)).unwrap();
    assert!(matches!(update, Update::Saved { result: Ok(()), .. }));
    assert!(started.elapsed() < Duration::from_millis(500));
    drop(commands);
    drop(updates);
    server.join().unwrap();
}
