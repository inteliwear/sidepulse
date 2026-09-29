//! Portable protocol checks for the presentation clients, including Windows pipes.

use std::fs;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use interprocess::local_socket::prelude::*;
use sidepulse_core::{
    AgentMode, ClientRequest, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload,
    VirtualDisplaySettingsPatch,
};

#[test]
fn history_and_virtual_clients_use_service_owned_state() {
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
        .join("view.sock")
        .to_string_lossy()
        .into_owned();
    #[cfg(windows)]
    let endpoint = format!("sidepulse-view-{id}");
    #[cfg(unix)]
    let _ = id;
    let path = directory.path().join("settings.json");
    let history = directory.path().join("status-history.jsonl");
    fs::write(&path, r#"{"unknown":7}"#).unwrap();
    let service = sidepulse_daemon::Service::new();
    service.configure_settings(&path).unwrap();
    service.configure_history(&history).unwrap();
    service.record_history(None).unwrap();
    let listener = sidepulse_ipc::bind(&endpoint).unwrap();
    let server = std::thread::spawn(move || {
        for _ in 0..6 {
            service
                .serve_connection(listener.accept().unwrap())
                .unwrap();
        }
    });
    let request = |kind| {
        let request = ClientRequest {
            version: PROTOCOL_VERSION,
            request_id: 1,
            kind,
        };
        let reply: ServerMessage =
            sidepulse_ipc::request(&endpoint, &request, Duration::from_secs(2)).unwrap();
        assert_eq!(reply.request_id, Some(1));
        reply.payload
    };
    assert!(matches!(
        request(RequestKind::SetHistoryTimeframe { seconds: 3600 }),
        ServerPayload::Settings { .. }
    ));
    let ServerPayload::History {
        points,
        timeframe_seconds,
        ..
    } = request(RequestKind::History)
    else {
        panic!("missing history");
    };
    assert_eq!(points.len(), 1);
    assert_eq!(timeframe_seconds, 3600);
    assert!(matches!(
        request(RequestKind::SetAgentAnimation {
            mode: AgentMode::IdleReady,
            style: "custom".into(),
            custom_program: Some("#FF0080".into())
        }),
        ServerPayload::Settings { .. }
    ));
    assert!(matches!(
        request(RequestKind::SetVirtualDisplay {
            patch: VirtualDisplaySettingsPatch {
                enabled: Some(true),
                ..Default::default()
            }
        }),
        ServerPayload::Settings { .. }
    ));
    let ServerPayload::VirtualDisplay { frame } = request(RequestKind::VirtualDisplay) else {
        panic!("missing frame");
    };
    assert!(frame.enabled);
    assert_eq!(frame.pixels, vec![[255, 0, 128]; 8]);
    assert!(matches!(
        request(RequestKind::SetVirtualDisplay {
            patch: VirtualDisplaySettingsPatch {
                enabled: Some(false),
                ..Default::default()
            }
        }),
        ServerPayload::Settings { .. }
    ));
    server.join().unwrap();
    let saved: serde_json::Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    assert_eq!(saved["unknown"], 7);
    assert_eq!(saved["history_timeframe_seconds"], 3600);
    assert_eq!(saved["virtual_status_device_enabled"], false);
    assert!(!directory.path().join("LEDS.LED").exists());
}
