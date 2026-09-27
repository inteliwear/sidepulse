use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use sidepulse_device::battery_diagnostics::{BatterySnapshot, read_battery_snapshot};

pub fn run_battery(mut args: impl Iterator<Item = String>) -> ExitCode {
    if args.next().as_deref() != Some("status") {
        eprintln!("usage: sidepulse-next battery status [--json] [--full-watts auto|WATTS]");
        return ExitCode::from(2);
    }
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
                let value = value.trim().to_lowercase();
                full_watts = if ["", "auto", "default"].contains(&value.as_str()) {
                    None
                } else {
                    match value.parse::<f64>() {
                        Ok(watts) if watts.is_finite() => Some(watts.max(1.0)),
                        _ => {
                            eprintln!("sidepulse-next battery status: invalid charger wattage");
                            return ExitCode::FAILURE;
                        }
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
