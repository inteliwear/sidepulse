use std::env;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

use sidepulse_core::{
    BatterySettingsPatch, ChargerBaseline, ClientRequest, PROTOCOL_VERSION, RequestKind,
    ServerMessage, ServerPayload,
};
use sidepulse_device::battery_diagnostics::{BatterySnapshot, read_battery_snapshot};

pub fn run_battery(mut args: impl Iterator<Item = String>) -> ExitCode {
    match args.next().as_deref() {
        Some("status") => run_status(args),
        Some("configure") => run_configure(args),
        _ => {
            eprintln!(
                "usage: sidepulse-next battery <status [--json] [--full-watts auto|WATTS] | configure --endpoint ENDPOINT [--display agent|battery|custom] [--full-watts auto|WATTS] [--show-on-power-change yes|no] [--power-change-preview-seconds SECONDS]>"
            );
            ExitCode::from(2)
        }
    }
}

fn run_status(mut args: impl Iterator<Item = String>) -> ExitCode {
    let mut json = false;
    let mut full_watts = saved_full_watts();
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--json" => json = true,
            "--full-watts" => {
                let Some(value) = args.next() else {
                    eprintln!("sidepulse-next battery status: --full-watts needs a value");
                    return ExitCode::from(2);
                };
                full_watts = match parse_full_watts(&value) {
                    Ok(ChargerBaseline::Auto) => None,
                    Ok(ChargerBaseline::Watts { watts }) => Some(watts),
                    Err(error) => {
                        eprintln!("sidepulse-next battery status: {error}");
                        return ExitCode::FAILURE;
                    }
                };
            }
            _ => {
                eprintln!("sidepulse-next battery status: unknown argument: {flag}");
                return ExitCode::from(2);
            }
        }
    }
    let snapshot = match read_battery_snapshot(full_watts) {
        Ok(snapshot) => snapshot,
        Err(error) => {
            eprintln!("sidepulse-next battery status: {error}");
            return ExitCode::FAILURE;
        }
    };
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&snapshot.legacy_json()).unwrap()
        );
    } else {
        println!("{}", render_snapshot(&snapshot));
    }
    ExitCode::SUCCESS
}

fn parse_full_watts(value: &str) -> Result<ChargerBaseline, &'static str> {
    let value = value.trim().to_lowercase();
    if ["", "auto", "default"].contains(&value.as_str()) {
        Ok(ChargerBaseline::Auto)
    } else {
        match value.parse::<f64>() {
            Ok(watts) if watts.is_finite() => Ok(ChargerBaseline::Watts {
                watts: watts.max(1.0),
            }),
            _ => Err("invalid charger wattage"),
        }
    }
}

fn run_configure(mut args: impl Iterator<Item = String>) -> ExitCode {
    let mut endpoint = env::var("SIDEPULSE_NEXT_ENDPOINT").ok();
    let mut patch = BatterySettingsPatch::default();
    let mut changed = false;
    while let Some(flag) = args.next() {
        if !matches!(
            flag.as_str(),
            "--endpoint"
                | "--display"
                | "--full-watts"
                | "--show-on-power-change"
                | "--power-change-preview-seconds"
        ) {
            eprintln!("sidepulse-next battery configure: unknown argument: {flag}");
            return ExitCode::from(2);
        }
        let Some(value) = args.next() else {
            eprintln!("sidepulse-next battery configure: {flag} needs a value");
            return ExitCode::from(2);
        };
        match flag.as_str() {
            "--endpoint" => endpoint = Some(value),
            "--display" => patch.display = Some(value),
            "--full-watts" => match parse_full_watts(&value) {
                Ok(baseline) => patch.full_charge_watts = Some(baseline),
                Err(error) => {
                    eprintln!("sidepulse-next battery configure: {error}");
                    return ExitCode::FAILURE;
                }
            },
            "--show-on-power-change" => match value.as_str() {
                "yes" => patch.show_on_power_change = Some(true),
                "no" => patch.show_on_power_change = Some(false),
                _ => {
                    eprintln!(
                        "sidepulse-next battery configure: power-change preview must be yes or no"
                    );
                    return ExitCode::from(2);
                }
            },
            "--power-change-preview-seconds" => match value.parse::<f64>() {
                Ok(seconds) if seconds.is_finite() => {
                    patch.power_change_preview_seconds = Some(seconds.max(0.0))
                }
                _ => {
                    eprintln!("sidepulse-next battery configure: invalid preview duration");
                    return ExitCode::FAILURE;
                }
            },
            _ => unreachable!(),
        }
        changed |= flag != "--endpoint";
    }
    if let Err(error) = patch.validate() {
        eprintln!("sidepulse-next battery configure: {error}");
        return ExitCode::from(2);
    }
    let Some(endpoint) = endpoint.filter(|endpoint| !endpoint.is_empty()) else {
        eprintln!(
            "sidepulse-next battery configure: use --endpoint or SIDEPULSE_NEXT_ENDPOINT to select the Rust service"
        );
        return ExitCode::from(2);
    };
    let request = ClientRequest {
        version: PROTOCOL_VERSION,
        request_id: 1,
        kind: if changed {
            RequestKind::SetBatterySettings { patch }
        } else {
            RequestKind::Settings
        },
    };
    let reply: ServerMessage =
        match sidepulse_ipc::request(&endpoint, &request, Duration::from_secs(2)) {
            Ok(reply) => reply,
            Err(error) => {
                eprintln!("sidepulse-next battery configure: {error}");
                return ExitCode::FAILURE;
            }
        };
    if reply.version != PROTOCOL_VERSION || reply.request_id != Some(1) {
        eprintln!("sidepulse-next battery configure: invalid service response");
        return ExitCode::FAILURE;
    }
    match reply.payload {
        ServerPayload::Settings { settings, .. } => {
            let battery = &settings["battery_monitoring"];
            let baseline = battery["full_charge_watts"]
                .as_f64()
                .map_or_else(|| "auto".into(), watts);
            let display = settings["led_display"].as_str().unwrap_or("agent");
            let preview = if battery["show_on_power_change"].as_bool().unwrap_or(true) {
                "on"
            } else {
                "off"
            };
            let seconds = battery["power_change_preview_seconds"]
                .as_f64()
                .unwrap_or(7.0);
            println!(
                "settings: Rust service\n  led display: {display}\n  full charge watts: {baseline}\n  power-change preview: {preview} ({seconds}s)"
            );
            if display == "battery" {
                println!("  status bar LEDs will show battery.");
            } else if display == "custom" {
                println!("  status bar LEDs will leave devices on manual output.");
            }
            ExitCode::SUCCESS
        }
        ServerPayload::Error { message, .. } => {
            eprintln!("sidepulse-next battery configure: {message}");
            ExitCode::FAILURE
        }
        _ => {
            eprintln!("sidepulse-next battery configure: unexpected service reply");
            ExitCode::FAILURE
        }
    }
}

fn saved_full_watts() -> Option<f64> {
    let home = env::var_os("HOME")
        .or_else(|| env::var_os("USERPROFILE"))
        .map(PathBuf::from)?;
    let root = env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .map(|path| {
            let text = path.to_string_lossy();
            if text == "~" {
                home.clone()
            } else if let Some(relative) = text.strip_prefix("~/") {
                home.join(relative)
            } else {
                path
            }
        })
        .unwrap_or_else(|| home.join(".config"));
    let document: serde_json::Value = serde_json::from_slice(
        &std::fs::read(root.join("sidepulse/agent-monitor/settings.json")).ok()?,
    )
    .ok()?;
    document
        .get("battery_monitoring")?
        .get("full_charge_watts")?
        .as_f64()
        .filter(|watts| watts.is_finite() && *watts > 0.0)
}

fn render_snapshot(snapshot: &BatterySnapshot) -> String {
    if !snapshot.battery_present {
        return "Battery: unavailable".into();
    }
    let state = if snapshot.is_charged {
        "charged"
    } else if snapshot.is_charging {
        "charging"
    } else if snapshot.is_plugged {
        "plugged in"
    } else {
        "on battery"
    };
    let mut lines = vec![format!("Battery: {}% ({state})", snapshot.percent)];
    if snapshot.is_plugged {
        lines.push(format!(
            "Adapter: {} of {} ({:.0}% speed)",
            watts(snapshot.adapter_power()),
            watts(snapshot.full_charge_watts),
            snapshot.charge_speed_ratio() * 100.0
        ));
    }
    if snapshot.battery_watts.abs() >= 0.1 {
        lines.push(format!(
            "Battery {}: {}",
            if snapshot.battery_watts > 0.0 {
                "charging"
            } else {
                "draining"
            },
            watts(snapshot.battery_watts.abs())
        ));
    }
    if snapshot.time_to_full > 0 && snapshot.is_charging {
        lines.push(format!("Time to full: {}", minutes(snapshot.time_to_full)));
    } else if snapshot.time_to_empty > 0 && !snapshot.is_plugged {
        lines.push(format!(
            "Time remaining: {}",
            minutes(snapshot.time_to_empty)
        ));
    }
    if snapshot.health_percent >= 0 {
        lines.push(format!("Health: {}%", snapshot.health_percent));
    }
    if snapshot.cycle_count >= 0 {
        lines.push(format!("Cycle count: {}", snapshot.cycle_count));
    }
    if !snapshot.condition.is_empty() {
        lines.push(format!("Condition: {}", snapshot.condition));
    }
    lines.join("\n")
}

fn watts(value: f64) -> String {
    if value <= 0.0 {
        "0W".into()
    } else if value >= 10.0 {
        format!("{value:.0}W")
    } else {
        format!("{value:.1}W")
    }
}

fn minutes(value: i64) -> String {
    if value < 60 {
        format!("{value}m")
    } else {
        format!("{}h{:02}m", value / 60, value % 60)
    }
}
