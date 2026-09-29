//! Rendering and output use shared headless modules, or the selected service.
//! The CLI owns the refresh loop and terminal presentation.
use crate::delivery::request;
use sidepulse_core::{DeliveryRequest, DestinationKind, RequestKind, ServerPayload};
use std::{
    collections::BTreeMap,
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
    let mut endpoint = crate::preview_endpoint();
    let mut explicit_endpoint = false;
    let mut status_args = Vec::new();
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
            "--help" | "-h" => {
                println!(
                    "Usage: sidepulse agent-monitor leds [--endpoint ENDPOINT] [--device PATH] [--file-name NAME] [--dry-run] [--once] [--interval SECONDS]\n       Source log and monitoring-policy options select standalone monitoring.\n       Battery LEDs also accept --full-watts auto|WATTS."
                );
                return ExitCode::SUCCESS;
            }
            "--once" => once = true,
            "--dry-run" => dry_run = true,
            "--all" if source == "agent" => status_args.push(arg),
            "--all-destinations" => delivery.send_all = true,
            "--endpoint" | "--device" | "--file-name" | "--to" | "--interval" | "--full-watts" => {
                let Some(value) = args.next() else {
                    return fail(format!("{arg} needs a value"));
                };
                match arg.as_str() {
                    "--endpoint" => {
                        endpoint = Some(value);
                        explicit_endpoint = true;
                    }
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
            _ if source == "agent"
                && ([
                    "--stale-after",
                    "--tool-running-timeout",
                    "--max-lines",
                    "--codex-transcripts",
                    "--claude-transcripts",
                ]
                .contains(&arg.as_str())
                    || sidepulse_sources::PROVIDERS
                        .iter()
                        .any(|provider| arg == format!("--{provider}-log"))) =>
            {
                let Some(value) = args.next() else {
                    return fail(format!("{arg} needs a value"));
                };
                status_args.extend([arg, value]);
            }
            _ => return fail(format!("unexpected argument {arg}")),
        }
    }
    let requires_detached = status_args.iter().any(|arg| arg != "--all");
    if explicit_endpoint && requires_detached {
        return fail(
            "source log and policy options apply to standalone monitoring; omit --endpoint to use them",
        );
    }
    if requires_detached {
        endpoint = None;
    }
    if endpoint.is_none() {
        return run_detached(
            source,
            status_args,
            delivery,
            once,
            dry_run,
            interval,
            full_watts,
        );
    }
    let endpoint = endpoint.unwrap();
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

fn run_detached(
    source: &str,
    status_args: Vec<String>,
    delivery: DeliveryRequest,
    once: bool,
    dry_run: bool,
    interval: f64,
    full_watts: Option<sidepulse_core::ChargerBaseline>,
) -> ExitCode {
    let mut monitor = if source == "agent" {
        match crate::status::DetachedMonitor::new(status_args) {
            Ok(monitor) => Some(monitor),
            Err(error) => return fail(error),
        }
    } else {
        None
    };
    let stop = Arc::new(AtomicBool::new(false));
    let signal = stop.clone();
    if let Err(error) = ctrlc::set_handler(move || signal.store(true, Ordering::Release)) {
        return fail(error);
    }
    let mut outputs = BTreeMap::<String, sidepulse_device::DeviceOutput>::new();
    let mut previews = BTreeMap::<String, String>::new();
    loop {
        if stop.load(Ordering::Acquire) {
            return ExitCode::SUCCESS;
        }
        let result = (|| -> Result<(), String> {
            let destinations = sidepulse_device::delivery::local_destinations(
                &delivery,
                &sidepulse_device::default_mount_roots(),
            )
            .map_err(|e| e.to_string())?;
            let targets = sidepulse_core::select_delivery_targets(&delivery, &destinations)
                .map_err(|e| e.to_string())?;
            let ids = targets
                .iter()
                .map(|target| target.id.clone())
                .collect::<Vec<_>>();
            outputs.retain(|id, _| ids.contains(id));
            previews.retain(|id, _| ids.contains(id));
            let mode = monitor
                .as_mut()
                .map(|monitor| monitor.snapshot().map(|snapshot| snapshot.aggregate.mode))
                .transpose()
                .map_err(|e| e.to_string())?;
            let battery = if source == "battery" {
                let watts = match full_watts {
                    Some(sidepulse_core::ChargerBaseline::Watts { watts }) => Some(watts),
                    Some(sidepulse_core::ChargerBaseline::Auto) => None,
                    None => crate::battery::saved_full_watts(),
                };
                Some(
                    sidepulse_device::battery_diagnostics::read_battery_snapshot(watts)
                        .map_err(|e| e.to_string())?
                        .led_state()
                        .ok_or("battery data is unavailable on this system")?,
                )
            } else {
                None
            };
            for target in targets {
                if stop.load(Ordering::Acquire) {
                    break;
                }
                let path = Path::new(&target.address);
                let count = sidepulse_device::led_count_for_target(path);
                let program = if let Some(mode) = mode {
                    sidepulse_device::program_for_mode(mode, count, 255)
                } else {
                    sidepulse_device::battery::program_for_battery(
                        battery.unwrap(),
                        count,
                        360,
                        255,
                    )
                };
                sidepulse_device::led_runtime::validate_program(&program, count)
                    .map_err(|e| e.to_string())?;
                if dry_run {
                    if once || previews.get(&target.id) != Some(&program) {
                        println!(
                            "{}",
                            serde_json::json!({"destination":target,"program":program,"action":"would_write"})
                        );
                    }
                    previews.insert(target.id, program);
                } else {
                    let output = outputs
                        .entry(target.id.clone())
                        .or_insert_with(|| sidepulse_device::DeviceOutput::with_target(path, 255));
                    if output.sync_program(&program).map_err(|e| e.to_string())? {
                        println!(
                            "{}",
                            serde_json::json!({"destination":target,"action":"written"})
                        );
                    }
                    output
                        .poke_keepalive(Instant::now())
                        .map_err(|e| e.to_string())?;
                }
            }
            io::stdout().flush().map_err(|e| e.to_string())?;
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
        let deadline = Instant::now() + Duration::from_secs_f64(interval);
        while !stop.load(Ordering::Acquire) && Instant::now() < deadline {
            std::thread::sleep(
                Duration::from_millis(100).min(deadline.saturating_duration_since(Instant::now())),
            );
        }
    }
}
