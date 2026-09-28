//! Exercise the actual presentation code without starting a window or OS helper.
use super::*;
use eframe::App;

struct Harness {
    app: SettingsApp,
    ctx: egui::Context,
    commands: Receiver<WorkerCommand>,
    updates: Sender<Update>,
}

impl Harness {
    fn new() -> Self {
        let (commands, pending) = mpsc::channel();
        let (updates, received) = mpsc::channel();
        let app = SettingsApp::with_channels("headless-fixture".into(), commands, received);
        let ctx = egui::Context::default();
        ctx.enable_accesskit();
        Self {
            app,
            ctx,
            commands: pending,
            updates,
        }
    }

    fn draw(&mut self, events: Vec<egui::Event>, size: [f32; 2]) -> egui::FullOutput {
        self.ctx.run_ui(
            egui::RawInput {
                screen_rect: Some(egui::Rect::from_min_size(egui::Pos2::ZERO, size.into())),
                events,
                ..Default::default()
            },
            |ui| self.app.ui(ui, &mut eframe::Frame::_new_kittest()),
        )
    }

    fn click(&mut self, node: &egui::accesskit::Node) {
        let bounds = node.bounds().expect("control bounds");
        let pos = egui::pos2(
            ((bounds.x0 + bounds.x1) / 2.0) as f32,
            ((bounds.y0 + bounds.y1) / 2.0) as f32,
        );
        self.draw(
            vec![
                egui::Event::PointerMoved(pos),
                egui::Event::PointerButton {
                    pos,
                    button: egui::PointerButton::Primary,
                    pressed: true,
                    modifiers: Default::default(),
                },
            ],
            [720.0, 600.0],
        );
        self.draw(
            vec![egui::Event::PointerButton {
                pos,
                button: egui::PointerButton::Primary,
                pressed: false,
                modifiers: Default::default(),
            }],
            [720.0, 600.0],
        );
    }

    fn connected(&mut self) {
        self.updates
            .send(Update::State(Ok(Box::new(fixture()))))
            .unwrap();
        self.draw(vec![], [720.0, 600.0]);
    }
}

fn fixture() -> ServiceState {
    let settings = SettingsView::from_service_payload(&ServerPayload::Settings {
        settings: serde_json::json!({"show_menu_bar_icon":true}),
        active_device: None,
        brightness: None,
        display_mode: None,
    })
    .unwrap();
    let setup = sidepulse_core::HookSetupStatus {
        configured: true,
        stage_dir: Some("/fixture/preview".into()),
        startup_directory: Some("/fixture/login".into()),
        home: "/fixture/home".into(),
        providers: ["codex", "claude", "grok", "cursor", "junie"]
            .map(|provider| sidepulse_core::ProviderHookStatus {
                provider: provider.into(),
                config_path: format!("/fixture/{provider}.json"),
                log_path: format!("/fixture/{provider}.jsonl"),
                installed: false,
                native: false,
                error: None,
            })
            .into(),
    };
    ServiceState {
        setup,
        settings,
        diagnostics: sidepulse_core::DiagnosticsStatus {
            settings_path: Some("/fixture/settings.json".into()),
            audit_path: Some("/fixture/event-status.jsonl".into()),
            audit_paths: vec!["/fixture/event-status.jsonl".into()],
            audit_bytes: 42,
            history_path: Some("/fixture/status-history.jsonl".into()),
            export_directory: Some("/fixture/exports".into()),
        },
        activity: TrayState::disconnected(),
        agents: vec![],
        devices: vec![],
        active_device: None,
        animation_choices: vec![],
        animation_states: vec![],
        animation_library: sidepulse_core::AnimationLibrary {
            profiles: Default::default(),
            custom_animations: Default::default(),
            current: Default::default(),
            matching_profile: None,
        },
        history_points: ["2026-09-28T00:00:00Z", "2026-09-28T00:30:00Z"]
            .map(|time| sidepulse_core::HistoryPoint {
                recorded_at: time.parse().unwrap(),
                agent_status: AgentMode::Working,
                display_status: "working".into(),
                battery_level: Some(75.0),
                charger_power_watts: Some(90.0),
                lid_closed: Some(false),
                keep_awake_active: Some(false),
                keep_awake_requested: Some(true),
                mac_sleep_prevented: Some(false),
            })
            .into(),
        history_timeframe: 3600,
        history_sampled: false,
        lid_durations: [1.0, 1.3],
        relay: sidepulse_core::RelaySettings {
            configured: true,
            server: "http://localhost/fixture".into(),
            machine_name: "Fixture".into(),
            receiver_code: String::new(),
            outbound_code: String::new(),
            last_received_at: None,
            last_sent_at: None,
            receive_error: None,
            send_error: None,
        },
        phones_configured: true,
        phone_output_enabled: false,
        phones: vec![],
        phone_pairing: None,
        power_control: sidepulse_core::PowerControlStatus {
            supported: cfg!(target_os = "macos"),
            ..Default::default()
        },
    }
}

fn nodes(output: &egui::FullOutput, label: &str) -> Vec<egui::accesskit::Node> {
    output
        .platform_output
        .accesskit_update
        .as_ref()
        .unwrap()
        .nodes
        .iter()
        .filter(|(_, node)| node.label() == Some(label))
        .map(|(_, node)| node.clone())
        .collect()
}

#[test]
fn setup_hooks_send_service_commands_and_offline_recovery_remains_enabled() {
    let mut h = Harness::new();
    h.connected();
    h.app.page = Page::Setup;
    let output = h.draw(vec![], [720.0, 600.0]);
    let mut installs = nodes(&output, "Install");
    installs.sort_by(|a, b| a.bounds().unwrap().y0.total_cmp(&b.bounds().unwrap().y0));
    assert_eq!(installs.len(), 5);
    h.click(&installs[0]);
    assert!(
        matches!(h.commands.try_recv().unwrap(), WorkerCommand::Request(RequestKind::ConfigureHooks {provider, install:true, dry_run:false}) if provider=="codex")
    );
    h.updates.send(Update::Managed(Ok("Saved".into()))).unwrap();
    h.updates
        .send(Update::State(Err("offline".into())))
        .unwrap();
    let output = h.draw(vec![], [720.0, 600.0]);
    assert!(
        nodes(&output, "Install")
            .iter()
            .all(|node| node.is_disabled())
    );
    let mut starts = nodes(&output, "Start now");
    starts.sort_by(|a, b| a.bounds().unwrap().y0.total_cmp(&b.bounds().unwrap().y0));
    assert_eq!(starts.len(), 2);
    assert!(starts.iter().all(|node| !node.is_disabled()));
    h.click(&starts[0]);
    assert!(matches!(
        h.commands.try_recv().unwrap(),
        WorkerCommand::Startup {
            job: Job::Service,
            operation: Operation::Start,
            dry_run: false
        }
    ));
}

#[test]
fn diagnostics_exports_and_tray_preferences_use_service_commands() {
    let mut h = Harness::new();
    h.connected();
    h.app.page = Page::Diagnostics;
    let output = h.draw(vec![], [720.0, 600.0]);
    let exports = nodes(&output, "Export CSV");
    assert_eq!(exports.len(), 1);
    h.click(&exports[0]);
    assert!(matches!(
        h.commands.try_recv().unwrap(),
        WorkerCommand::Request(RequestKind::ExportDiagnostics {
            format: sidepulse_core::DiagnosticFormat::Csv
        })
    ));
    h.updates
        .send(Update::Exported(Ok(("/fixture/report.csv".into(), 3))))
        .unwrap();
    let output = h.draw(vec![], [720.0, 600.0]);
    assert_eq!(h.app.last_export.as_deref(), Some("/fixture/report.csv"));
    assert_eq!(nodes(&output, "Open export").len(), 1);
    h.app.page = Page::Monitoring;
    let output = h.draw(vec![], [720.0, 600.0]);
    let visibility = nodes(&output, "Show status bar icon");
    assert_eq!(visibility.len(), 1);
    h.click(&visibility[0]);
    assert!(matches!(
        h.commands.try_recv().unwrap(),
        WorkerCommand::Request(RequestKind::SetTrayVisibility { visible: false })
    ));
}

#[test]
fn every_page_draws_at_supported_window_sizes_without_os_actions() {
    let mut h = Harness::new();
    h.connected();
    for size in [[720.0, 600.0], [580.0, 420.0]] {
        for page in [
            Page::Activity,
            Page::Devices,
            Page::Battery,
            Page::Monitoring,
            Page::Sleep,
            Page::Animations,
            Page::History,
            Page::Sessions,
            Page::Relay,
            Page::Phones,
            Page::Setup,
            Page::Diagnostics,
        ] {
            h.app.page = page;
            let output = h.draw(vec![], size);
            assert!(!output.shapes.is_empty());
            assert!(h.commands.try_recv().is_err());
            for (_, node) in &output
                .platform_output
                .accesskit_update
                .as_ref()
                .unwrap()
                .nodes
            {
                if let Some(bounds) = node.bounds() {
                    assert!(
                        bounds.x0 >= -1.0 && bounds.x1 <= f64::from(size[0]) + 1.0,
                        "{page:?} at {size:?}: {:?} extends outside window: {bounds:?}",
                        node.label()
                    );
                }
            }
        }
    }
}

#[test]
fn history_keeps_all_six_rows_inside_the_chart_at_minimum_width() {
    let mut h = Harness::new();
    h.connected();
    h.app.page = Page::History;
    // Tall input makes the entire scrollable chart available for geometry checks.
    let output = h.draw(vec![], [580.0, 900.0]);
    let labels = [
        "Battery\n0–100%",
        "Charger\n0–90 W",
        "Agent status",
        "SidePulse awake",
        "Mac sleep",
        "Lid",
    ];
    let mut rows = vec![];
    for label in labels {
        let text = output
            .shapes
            .iter()
            .find_map(|shape| match &shape.shape {
                egui::epaint::Shape::Text(text) if text.galley.job.text == label => Some(text),
                _ => None,
            })
            .unwrap_or_else(|| panic!("missing history row {label}"));
        let bounds = egui::Rect::from_min_size(text.pos, text.galley.size());
        assert!(
            bounds.left() >= 0.0 && bounds.right() < 580.0,
            "{label}: {bounds:?}"
        );
        rows.push(bounds);
    }
    for pair in rows.windows(2) {
        assert!(
            pair[0].bottom() < pair[1].top(),
            "history labels overlap: {pair:?}"
        );
    }
    assert!(h.commands.try_recv().is_err());
}
