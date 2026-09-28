//! The CLI requests rendering and delivery from the service. It owns only the
//! refresh loop and terminal presentation.
use crate::delivery::request;
use sidepulse_core::{DeliveryRequest, DestinationKind, RequestKind, ServerPayload};
use std::{
    collections::BTreeMap,
    env,
    io::{self, Write},
    path::Path,
    process::ExitCode,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
fn fail(error: impl std::fmt::Display) -> ExitCode {
    eprintln!("sidepulse-next leds: {error}");
    ExitCode::from(2)
}
pub fn run_leds(source: &str, mut args: impl Iterator<Item = String>) -> ExitCode {
    let mut endpoint = env::var("SIDEPULSE_NEXT_ENDPOINT").ok();
    let mut delivery = DeliveryRequest {
        program: Some("off".into()),
        dry_run: true,
        ..Default::default()
    };
    let mut once = false;
    let mut dry_run = false;
    let mut interval = 1.0;
    let mut full_watts = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--once" => once = true,
            "--dry-run" => dry_run = true,
            "--all" if source == "agent" => {}
            "--all-destinations" => delivery.send_all = true,
            "--endpoint" | "--device" | "--file-name" | "--to" | "--interval" | "--full-watts" => {
                let Some(value) = args.next() else {
                    return fail(format!("{arg} needs a value"));
                };
                match arg.as_str() {
                    "--endpoint" => endpoint = Some(value),
                    "--device" => delivery.device = Some(value),
                    "--file-name" => delivery.file_name = Some(value),
                    "--to" => delivery.requested = Some(value),
                    "--interval" => match value.parse::<f64>() {
                        Ok(value) if value.is_finite() && (0.1..=3600.0).contains(&value) => {
                            interval = value
                        }
                        _ => return fail("interval must be between 0.1 and 3600 seconds"),
                    },
                    "--full-watts" if source == "battery" => {
                        full_watts = match crate::battery::parse_full_watts(&value) {
                            Ok(value) => Some(value),
                            Err(error) => return fail(error),
                        };
                    }

                    _ => return fail(format!("unsupported option {arg}")),
                }
            }
            _ => return fail(format!("unexpected argument {arg}")),
        }
    }
    let Some(endpoint) = endpoint else {
        return fail("provide --endpoint ENDPOINT, or set SIDEPULSE_NEXT_ENDPOINT");
    };
    let stop = Arc::new(AtomicBool::new(false));
    let signal = stop.clone();
    if let Err(error) = ctrlc::set_handler(move || signal.store(true, Ordering::Release)) {
        return fail(error);
    }
    let mut last_programs = BTreeMap::new();
    loop {
        if stop.load(Ordering::Acquire) {
            return ExitCode::SUCCESS;
        }
        let result = (|| -> Result<(), String> {
            let ServerPayload::Delivery { outcomes } = request(
                &endpoint,
                RequestKind::Deliver {
                    request: delivery.clone(),
                },
            )?
            else {
                return Err("the service did not return destinations".into());
            };
            let active: Vec<_> = outcomes
                .iter()
                .map(|outcome| outcome.destination.id.clone())
                .collect();
            last_programs.retain(|id, _| active.contains(id));
            for outcome in outcomes {
                let destination = outcome.destination;
                let led_count = if destination.kind == DestinationKind::Phone {
                    8
                } else {
                    sidepulse_device::led_count_for_target(Path::new(&destination.address)) as u8
                };
                let ServerPayload::LedProgram { program } = request(
                    &endpoint,
                    RequestKind::RenderLedProgram {
                        source: source.into(),
                        led_count,
                        full_watts,
                    },
                )?
                else {
                    return Err("the service did not return an LED program".into());
                };
                if !once && last_programs.get(&destination.id) == Some(&program) {
                    continue;
                }
                if dry_run {
                    println!(
                        "{}",
                        serde_json::json!({"destination":destination,"program":program,"action":"would_write"})
                    );
                } else {
                    let payload = request(
                        &endpoint,
                        RequestKind::Deliver {
                            request: DeliveryRequest {
                                program: Some(program.clone()),
                                device: (destination.kind == DestinationKind::Local).then(|| {
                                    Path::new(&destination.address)
                                        .parent()
                                        .unwrap_or(Path::new("."))
                                        .to_string_lossy()
                                        .into_owned()
                                }),
                                file_name: (destination.kind == DestinationKind::Local).then(
                                    || {
                                        Path::new(&destination.address)
                                            .file_name()
                                            .unwrap_or_default()
                                            .to_string_lossy()
                                            .into_owned()
                                    },
                                ),
                                requested: (destination.kind == DestinationKind::Phone)
                                    .then(|| destination.id.clone()),
                                ..Default::default()
                            },
                        },
                    )?;
                    let ServerPayload::DeliveryJob { job } = payload else {
                        return Err("the service did not return a delivery job".into());
                    };
                    let started = Instant::now();
                    loop {
                        if stop.load(Ordering::Acquire) {
                            return Ok(());
                        }
                        let ServerPayload::DeliveryJob { job } = request(
                            &endpoint,
                            RequestKind::DeliveryStatus { id: job.id.clone() },
                        )?
                        else {
                            return Err("the service did not return delivery status".into());
                        };
                        if job.state != "running" {
                            if job.state != "completed" {
                                return Err(job
                                    .outcomes
                                    .into_iter()
                                    .filter_map(|outcome| outcome.error)
                                    .collect::<Vec<_>>()
                                    .join("; "));
                            }
                            println!(
                                "{}",
                                serde_json::json!({"destination":destination,"action":"written"})
                            );
                            break;
                        }
                        if started.elapsed() > Duration::from_secs(30) {
                            return Err("delivery is still running".into());
                        }
                        std::thread::sleep(Duration::from_millis(50));
                    }
                }
                last_programs.insert(destination.id, program);
                let _ = io::stdout().flush();
            }
            Ok(())
        })();
        if let Err(error) = result {
            if once {
                return fail(error);
            }
            eprintln!("sidepulse-next leds: {error}");
        }
        if once {
            return ExitCode::SUCCESS;
        }
        let next = Instant::now() + Duration::from_secs_f64(interval);
        while !stop.load(Ordering::Acquire) && Instant::now() < next {
            std::thread::sleep(
                Duration::from_millis(100).min(next.saturating_duration_since(Instant::now())),
            );
        }
    }
}
