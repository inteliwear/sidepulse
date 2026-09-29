use sidepulse_core::{
    ClientRequest, DeliveryRequest, PROTOCOL_VERSION, RequestKind, ServerMessage, ServerPayload,
};
use std::{
    io::{self, IsTerminal, Read},
    process::ExitCode,
    time::Duration,
};

pub(super) fn request(endpoint: &str, kind: RequestKind) -> Result<ServerPayload, String> {
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind,
    };
    request.validate().map_err(str::to_owned)?;
    let response: ServerMessage =
        sidepulse_ipc::request(endpoint, &request, Duration::from_secs(3))
            .map_err(|error| error.to_string())?;
    if response.version != PROTOCOL_VERSION || response.request_id != Some(1) {
        return Err("invalid service response".into());
    }
    match response.payload {
        ServerPayload::Error { message, .. } => Err(message),
        payload => Ok(payload),
    }
}
fn finish(result: Result<(), String>, command: &str) -> ExitCode {
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("sidepulse-next {command}: {error}");
            ExitCode::FAILURE
        }
    }
}
fn endpoint(value: Option<String>) -> Result<String, String> {
    value
        .or_else(crate::preview_endpoint)
        .ok_or("provide --endpoint ENDPOINT, or set SIDEPULSE_NEXT_ENDPOINT".into())
}
pub fn run_delivery(command: &str, args: impl Iterator<Item = String>) -> ExitCode {
    let mut delivery = DeliveryRequest {
        prefer_phone: command == "push",
        ..Default::default()
    };
    let mut service = None;
    let mut args = args.peekable();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--all" => delivery.send_all = true,
            "--dry-run" => delivery.dry_run = true,
            "--endpoint" | "--title" | "--message" | "--to" | "--device" | "--file-name" => {
                let Some(value) = args.next() else {
                    return finish(Err(format!("{arg} requires a value")), command);
                };
                match arg.as_str() {
                    "--endpoint" => service = Some(value),
                    "--title" => delivery.title = Some(value),
                    "--message" => delivery.message = Some(value),
                    "--to" => delivery.requested = Some(value),
                    "--device" => delivery.device = Some(value),
                    "--file-name" => delivery.file_name = Some(value),
                    _ => unreachable!(),
                }
            }
            _ if (arg.starts_with('-') && arg != "-") || delivery.program.is_some() => {
                return finish(Err(format!("unexpected argument {arg}")), command);
            }
            _ => delivery.program = Some(arg),
        }
    }
    if delivery.program.as_deref() == Some("-")
        || (delivery.program.is_none()
            && !delivery.has_notification()
            && !io::stdin().is_terminal())
    {
        let mut input = String::new();
        if let Err(error) = io::stdin().take(65537).read_to_string(&mut input) {
            return finish(Err(error.to_string()), command);
        }
        if input.len() > 65536 {
            return finish(Err("LED input is too large".into()), command);
        }
        delivery.program = Some(input);
    }
    finish(
        (|| {
            delivery.validate().map_err(str::to_owned)?;
            let service = endpoint(service)?;
            let mut payload = request(&service, RequestKind::Deliver { request: delivery })?;
            let started = std::time::Instant::now();
            loop {
                match payload {
                    ServerPayload::Delivery { outcomes } => {
                        println!("{}", serde_json::to_string_pretty(&outcomes).unwrap());
                        return Ok(());
                    }
                    ServerPayload::DeliveryJob { job } if job.state != "running" => {
                        println!("{}", serde_json::to_string_pretty(&job).unwrap());
                        return if job.state == "completed" {
                            Ok(())
                        } else {
                            Err("one or more deliveries failed".into())
                        };
                    }
                    ServerPayload::DeliveryJob { job } => {
                        if started.elapsed() >= Duration::from_secs(300) {
                            return Err(format!("delivery is still running; check job {}", job.id));
                        }
                        std::thread::sleep(Duration::from_millis(100));
                        payload = request(&service, RequestKind::DeliveryStatus { id: job.id })?;
                    }
                    _ => return Err("unexpected delivery response".into()),
                }
            }
        })(),
        command,
    )
}

pub fn run_phone_link(args: impl Iterator<Item = String>) -> ExitCode {
    let mut args = args.peekable();
    let operation = args.next().unwrap_or_else(|| "pair".into());
    let mut service = None;
    let mut server = None;
    let mut token = None;
    let mut name = "iPhone".to_owned();
    let mut id = None;
    let mut display = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--endpoint" | "--server" | "--token" | "--name" => {
                let Some(value) = args.next() else { return finish(Err(format!("{arg} requires a value")), "phone-link"); };
                match arg.as_str() { "--endpoint" => service=Some(value), "--server" => server=Some(value), "--token" => token=Some(value), "--name" => name=value, _=>unreachable!() }
            }
            _ if matches!(operation.as_str(), "remove"|"display") && id.is_none() => id=Some(arg),
            _ if operation == "display" && display.is_none() => display=Some(arg),
            _ => return finish(Err("usage: phone-link pair|list|cancel|reload|register --token TOKEN|remove ID|display ID agent|battery|custom [--endpoint ENDPOINT] [--server ORIGIN] [--name NAME]".into()), "phone-link"),
        }
    }
    finish(
        (|| {
            let service = endpoint(service)?;
            let kind = match operation.as_str() {
                "list" => RequestKind::PhoneLinks,
                "pair" => RequestKind::BeginPhonePairing { server },
                "cancel" => RequestKind::CancelPhonePairing,
                "reload" => RequestKind::ReloadPhoneLinks,
                "remove" => RequestKind::RemovePhone {
                    id: id.ok_or("provide a phone ID")?,
                },
                "display" => RequestKind::SetPhoneDisplay {
                    id: id.ok_or("provide a phone ID")?,
                    display: display.ok_or("provide agent, battery, or custom")?,
                },
                "register" => RequestKind::RegisterPhone {
                    token: token.ok_or("provide --token TOKEN")?,
                    name,
                    server,
                },
                _ => return Err("unknown phone-link operation".into()),
            };
            let payload = request(&service, kind)?;
            if let ServerPayload::PhoneLinks {
                pairing: Some(pairing),
                ..
            } = &payload
                && pairing.state == "awaiting"
            {
                println!(
                    "Scan this code with SidePulse on your phone:\n{}",
                    pairing.url
                );
                for row in &pairing.qr {
                    println!(
                        "{}\x1b[0m",
                        row.iter()
                            .map(|dark| if *dark { "\x1b[40m  " } else { "\x1b[47m  " })
                            .collect::<String>()
                    );
                }
            }
            println!("{}", serde_json::to_string_pretty(&payload).unwrap());
            Ok(())
        })(),
        "phone-link",
    )
}
