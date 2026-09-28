//! CLI access to the same setup and debug export API used by settings.
use sidepulse_core::{
    ClientRequest, DiagnosticFormat, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload,
};
use std::{process::ExitCode, time::Duration};

pub fn run(command: &str, args: impl Iterator<Item = String>) -> ExitCode {
    let args = args.collect::<Vec<_>>();
    let values = args.iter().map(String::as_str).collect::<Vec<_>>();
    let parsed = match (command, values.as_slice()) {
        ("setup-status", [endpoint]) => Some((*endpoint, RequestKind::HookSetup)),
        ("tray-visibility", [endpoint, visibility]) if matches!(*visibility, "show" | "hide") => {
            Some((
                *endpoint,
                RequestKind::SetTrayVisibility {
                    visible: *visibility == "show",
                },
            ))
        }
        ("diagnostics", [endpoint]) => Some((*endpoint, RequestKind::Diagnostics)),
        ("diagnostics-export", [endpoint, format]) => match *format {
            "csv" => Some((
                *endpoint,
                RequestKind::ExportDiagnostics {
                    format: DiagnosticFormat::Csv,
                },
            )),
            "html" => Some((
                *endpoint,
                RequestKind::ExportDiagnostics {
                    format: DiagnosticFormat::Html,
                },
            )),
            _ => None,
        },
        ("configure-hooks", [endpoint, provider, action])
        | ("configure-hooks", [endpoint, provider, action, "--dry-run"])
            if matches!(*action, "install" | "remove" | "uninstall") =>
        {
            Some((
                *endpoint,
                RequestKind::ConfigureHooks {
                    provider: (*provider).into(),
                    install: *action == "install",
                    dry_run: values.len() == 4,
                },
            ))
        }
        _ => None,
    };
    let Some((endpoint, kind)) = parsed else {
        eprintln!(
            "Usage: sidepulse setup-status ENDPOINT\n       sidepulse configure-hooks ENDPOINT PROVIDER install|remove [--dry-run]\n       sidepulse tray-visibility ENDPOINT show|hide\n       sidepulse diagnostics ENDPOINT\n       sidepulse diagnostics-export ENDPOINT csv|html"
        );
        return ExitCode::from(2);
    };
    let timeout = if matches!(kind, RequestKind::ExportDiagnostics { .. }) {
        Duration::from_secs(30)
    } else {
        Duration::from_secs(3)
    };
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind,
    };
    match sidepulse_ipc::request::<_, ServerMessage>(endpoint, &request, timeout) {
        Ok(reply) if reply.version == PROTOCOL_VERSION && reply.request_id == Some(1) => {
            if let ServerPayload::Error { message, .. } = reply.payload {
                eprintln!("sidepulse: {message}");
                ExitCode::FAILURE
            } else {
                println!("{}", serde_json::to_string_pretty(&reply.payload).unwrap());
                ExitCode::SUCCESS
            }
        }
        Ok(_) => {
            eprintln!("sidepulse: invalid service response");
            ExitCode::FAILURE
        }
        Err(error) => {
            eprintln!("sidepulse: {error}");
            ExitCode::FAILURE
        }
    }
}
