//! Battery diagnostics and charger data, independent of any UI toolkit.

use std::io::{self, Cursor};
use std::sync::LazyLock;

use plist::{Dictionary, Value};
use regex::Regex;
use serde::Serialize;

use crate::battery::BatteryState;

#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
pub struct PdProfile {
    pub voltage: f64,
    pub current: f64,
    pub watts: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BatterySnapshot {
    pub percent: i32,
    pub is_charging: bool,
    pub is_charged: bool,
    pub is_plugged: bool,
    pub battery_present: bool,
    pub voltage: f64,
    pub amperage: f64,
    pub battery_watts: f64,
    pub time_to_full: i64,
    pub time_to_empty: i64,
    pub cycle_count: i64,
    pub temperature_c: Option<f64>,
    pub design_capacity_mah: i64,
    pub max_capacity_mah: i64,
    pub current_capacity_mah: i64,
    pub health_percent: i64,
    pub condition: String,
    pub adapter_connected: bool,
    pub adapter_watts: i64,
    pub adapter_voltage: f64,
    pub adapter_current: f64,
    pub adapter_name: String,
    pub adapter_manufacturer: String,
    pub adapter_model: String,
    pub adapter_serial: String,
    pub adapter_family: String,
    pub pd_profiles: Vec<PdProfile>,
    pub full_charge_watts: f64,
}

impl Default for BatterySnapshot {
    fn default() -> Self {
        Self {
            percent: 0,
            is_charging: false,
            is_charged: false,
            is_plugged: false,
            battery_present: false,
            voltage: 0.0,
            amperage: 0.0,
            battery_watts: 0.0,
            time_to_full: -1,
            time_to_empty: -1,
            cycle_count: -1,
            temperature_c: None,
            design_capacity_mah: -1,
            max_capacity_mah: -1,
            current_capacity_mah: -1,
            health_percent: -1,
            condition: String::new(),
            adapter_connected: false,
            adapter_watts: -1,
            adapter_voltage: 0.0,
            adapter_current: 0.0,
            adapter_name: String::new(),
            adapter_manufacturer: String::new(),
            adapter_model: String::new(),
            adapter_serial: String::new(),
            adapter_family: String::new(),
            pd_profiles: Vec::new(),
            full_charge_watts: 100.0,
        }
    }
}

impl BatterySnapshot {
    pub fn adapter_power(&self) -> f64 {
        if self.adapter_watts > 0 {
            return self.adapter_watts as f64;
        }
        let negotiated = self.adapter_voltage * self.adapter_current;
        if negotiated > 0.0 {
            return negotiated;
        }
        self.pd_profiles
            .iter()
            .map(|profile| profile.watts)
            .fold(0.0, f64::max)
    }

    pub fn charge_speed_ratio(&self) -> f64 {
        if self.full_charge_watts > 0.0 {
            (self.adapter_power() / self.full_charge_watts).clamp(0.0, 1.0)
        } else {
            0.0
        }
    }

    pub fn led_state(&self) -> Option<BatteryState> {
        self.battery_present.then(|| BatteryState {
            percent: self.percent,
            is_plugged: self.is_plugged,
            is_charging: self.is_charging,
            is_charged: self.is_charged,
            adapter_watts: self.adapter_power(),
            full_charge_watts: self.full_charge_watts,
        })
    }

    pub fn legacy_json(&self) -> serde_json::Value {
        let mut value = serde_json::to_value(self).expect("battery snapshot serializes");
        let object = value
            .as_object_mut()
            .expect("battery snapshot is an object");
        object.insert("adapter_power".into(), self.adapter_power().into());
        object.insert(
            "charge_speed_ratio".into(),
            self.charge_speed_ratio().into(),
        );
        value
    }
}

pub fn parse_ioreg_battery_plist(
    data: &[u8],
    full_charge_watts: f64,
) -> io::Result<BatterySnapshot> {
    let value = Value::from_reader(Cursor::new(data))
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    let mut snapshot = value
        .as_array()
        .and_then(|rows| rows.first())
        .and_then(Value::as_dictionary)
        .map(snapshot_from_ioreg_props)
        .unwrap_or_default();
    snapshot.full_charge_watts = if full_charge_watts.is_finite() {
        full_charge_watts.max(1.0)
    } else {
        100.0
    };
    Ok(snapshot)
}

#[cfg(target_os = "macos")]
pub fn read_battery_snapshot(full_charge_watts: Option<f64>) -> io::Result<BatterySnapshot> {
    let output = bounded_command_output(
        std::process::Command::new("ioreg").args(["-r", "-n", "AppleSmartBattery", "-a"]),
        std::time::Duration::from_secs(2),
    )?;
    parse_ioreg_battery_plist(
        &output,
        full_charge_watts.unwrap_or_else(default_full_charge_watts),
    )
}

#[cfg(target_os = "macos")]
fn bounded_command_output(
    command: &mut std::process::Command,
    timeout: std::time::Duration,
) -> io::Result<Vec<u8>> {
    use std::io::Read;
    use std::process::Stdio;
    use std::time::Instant;

    const LIMIT: u64 = 4 * 1024 * 1024;
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let output = child.stdout.take().expect("child stdout is piped");
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        output.take(LIMIT + 1).read_to_end(&mut bytes)?;
        Ok::<_, io::Error>(bytes)
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Ok(status),
            Err(error) => break Err(error),
            Ok(None) if Instant::now() >= deadline => {
                break Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "battery query timed out",
                ));
            }
            Ok(None) => std::thread::sleep(std::time::Duration::from_millis(10)),
        }
    };
    if status.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    let output = reader
        .join()
        .map_err(|_| io::Error::other("battery query reader failed"))??;
    if !status?.success() {
        return Err(io::Error::other("ioreg could not read battery status"));
    }
    if output.len() as u64 > LIMIT {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "battery query output exceeds limit",
        ));
    }
    Ok(output)
}

#[cfg(not(target_os = "macos"))]
pub fn read_battery_snapshot(full_charge_watts: Option<f64>) -> io::Result<BatterySnapshot> {
    let mut snapshot = BatterySnapshot::default();
    if let Some(state) = crate::battery_source::read_battery_state()? {
        snapshot.percent = state.percent;
        snapshot.is_plugged = state.is_plugged;
        snapshot.is_charging = state.is_charging;
        snapshot.is_charged = state.is_charged;
        snapshot.battery_present = true;
    }
    if let Some(watts) = full_charge_watts.filter(|watts| watts.is_finite() && *watts > 0.0) {
        snapshot.full_charge_watts = watts;
    }
    Ok(snapshot)
}

pub fn baseline_from_product_tree(text: &str) -> Option<f64> {
    static SOC: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r#""product-soc-name"\s*=\s*<"([^"]+)">"#).unwrap());
    let chip = SOC
        .captures(text)
        .map(|capture| capture[1].to_lowercase())
        .unwrap_or_default();
    model_baseline(&text.to_lowercase(), &chip)
}

fn model_baseline(model: &str, chip: &str) -> Option<f64> {
    if model.contains("macbook pro") {
        if model.contains("16-inch") {
            return Some(140.0);
        }
        if model.contains("14-inch") {
            return Some(if chip.contains("pro") || chip.contains("max") {
                96.0
            } else {
                70.0
            });
        }
    }
    None
}

#[cfg(target_os = "macos")]
fn default_full_charge_watts() -> f64 {
    static BASELINE: LazyLock<f64> = LazyLock::new(|| {
        if let Ok(output) = std::process::Command::new("ioreg")
            .args(["-p", "IODeviceTree", "-r", "-d", "1", "-n", "product"])
            .output()
            && output.status.success()
            && let Some(watts) =
                baseline_from_product_tree(&String::from_utf8_lossy(&output.stdout))
        {
            return watts;
        }
        if let Ok(output) = std::process::Command::new("system_profiler")
            .args(["SPHardwareDataType", "-json"])
            .output()
            && output.status.success()
            && let Ok(data) = serde_json::from_slice::<serde_json::Value>(&output.stdout)
            && let Some(hardware) = data
                .get("SPHardwareDataType")
                .and_then(|value| value.get(0))
        {
            let chip = hardware
                .get("chip_type")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("")
                .to_lowercase();
            let model = ["machine_name", "machine_model", "model_number"]
                .iter()
                .filter_map(|key| hardware.get(key).and_then(serde_json::Value::as_str))
                .collect::<Vec<_>>()
                .join(" ")
                .to_lowercase();
            if let Some(watts) = model_baseline(&model, &chip) {
                return watts;
            }
        }
        100.0
    });
    *BASELINE
}

fn snapshot_from_ioreg_props(props: &Dictionary) -> BatterySnapshot {
    let voltage = integer(props.get("Voltage"), 0) as f64 / 1000.0;
    let mut amperage = integer(props.get("Amperage"), 0);
    if amperage == 0 {
        amperage = integer(props.get("InstantAmperage"), 0);
    }
    let amperage = amperage as f64 / 1000.0;
    let raw_max = integer(props.get("AppleRawMaxCapacity"), -1);
    let raw_current = integer(props.get("AppleRawCurrentCapacity"), -1);
    let design = integer(props.get("DesignCapacity"), -1);
    let mut percent = integer(props.get("CurrentCapacity"), 0);
    if !(0..=100).contains(&percent) {
        percent = integer(props.get("StateOfCharge"), 0);
    }
    let temperature = integer(props.get("Temperature"), -1);
    let installed = props
        .get("BatteryInstalled")
        .and_then(Value::as_boolean)
        .unwrap_or(true);
    let mut snapshot = BatterySnapshot {
        percent: percent.clamp(0, 100) as i32,
        is_charging: boolean(props.get("IsCharging")),
        is_charged: boolean(props.get("FullyCharged")),
        is_plugged: boolean(props.get("ExternalConnected")),
        battery_present: installed,
        voltage,
        amperage,
        battery_watts: voltage * amperage,
        time_to_full: integer(props.get("AvgTimeToFull"), -1),
        time_to_empty: integer(props.get("AvgTimeToEmpty"), -1),
        cycle_count: integer(props.get("CycleCount"), -1),
        temperature_c: (temperature >= 0).then_some(temperature as f64 / 100.0),
        design_capacity_mah: design,
        max_capacity_mah: if raw_max > 0 {
            raw_max
        } else {
            integer(props.get("MaxCapacity"), -1)
        },
        current_capacity_mah: if raw_current >= 0 {
            raw_current
        } else {
            integer(props.get("CurrentCapacity"), -1)
        },
        health_percent: if raw_max > 0 && design > 0 {
            (raw_max as f64 / design as f64 * 100.0).round_ties_even() as i64
        } else {
            -1
        },
        condition: if integer(props.get("PermanentFailureStatus"), 0) != 0 {
            "Service Recommended".into()
        } else if installed {
            "Normal".into()
        } else {
            String::new()
        },
        ..Default::default()
    };
    if let Some(details) = props.get("AdapterDetails").and_then(Value::as_dictionary) {
        snapshot.adapter_connected = !details.is_empty();
        snapshot.adapter_watts = integer(details.get("Watts"), -1);
        snapshot.adapter_voltage = integer(
            details
                .get("AdapterVoltage")
                .or_else(|| details.get("Voltage")),
            0,
        ) as f64
            / 1000.0;
        snapshot.adapter_current = integer(details.get("Current"), 0) as f64 / 1000.0;
        snapshot.adapter_name = string(details.get("Name"));
        snapshot.adapter_manufacturer = string(details.get("Manufacturer"));
        snapshot.adapter_model = string(details.get("Model"));
        snapshot.adapter_serial = string(
            details
                .get("SerialString")
                .or_else(|| details.get("SerialNumber")),
        );
        snapshot.adapter_family = adapter_family_name(integer(details.get("FamilyCode"), -1));
        if let Some(menu) = details.get("UsbHvcMenu").and_then(Value::as_array) {
            for entry in menu.iter().filter_map(Value::as_dictionary) {
                let voltage = integer(entry.get("MaxVoltage"), 0) as f64 / 1000.0;
                let current = integer(entry.get("MaxCurrent"), 0) as f64 / 1000.0;
                if voltage > 0.0 && current > 0.0 {
                    snapshot.pd_profiles.push(PdProfile {
                        voltage,
                        current,
                        watts: voltage * current,
                    });
                }
            }
            snapshot
                .pd_profiles
                .sort_by(|left, right| left.watts.total_cmp(&right.watts));
        }
    }
    snapshot
}

fn integer(value: Option<&Value>, default: i64) -> i64 {
    match value {
        Some(Value::Boolean(value)) => i64::from(*value),
        Some(Value::Integer(value)) => value
            .as_signed()
            .or_else(|| {
                value
                    .as_unsigned()
                    .and_then(|value| i64::try_from(value).ok())
            })
            .unwrap_or(default),
        Some(Value::Real(value)) if value.is_finite() => *value as i64,
        _ => default,
    }
}

fn boolean(value: Option<&Value>) -> bool {
    value
        .and_then(Value::as_boolean)
        .unwrap_or_else(|| integer(value, 0) != 0)
}

fn string(value: Option<&Value>) -> String {
    value.and_then(Value::as_string).unwrap_or("").to_owned()
}

fn adapter_family_name(code: i64) -> String {
    match code {
        0xe0004000..=0xe0004fff => "USB-C Power Delivery".into(),
        3 => "MagSafe".into(),
        4 => "MagSafe 2".into(),
        0xff00 => "USB-C".into(),
        code if code >= 0 => format!("0x{code:08x}"),
        _ => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn charging_diagnostics_match_the_python_snapshot() {
        let snapshot = parse_ioreg_battery_plist(
            include_bytes!("../resources/fixtures/battery-charging.plist"),
            140.0,
        )
        .unwrap();
        let expected: serde_json::Value = serde_json::from_str(include_str!(
            "../resources/fixtures/battery-charging.expected.json"
        ))
        .unwrap();
        assert_eq!(snapshot.legacy_json(), expected);
        let state = snapshot.led_state().unwrap();
        assert_eq!(state.adapter_watts, 96.0);
        assert_eq!(state.full_charge_watts, 140.0);
    }

    #[test]
    fn absent_battery_and_power_fallbacks_remain_explicit() {
        let missing = parse_ioreg_battery_plist(
            b"<?xml version=\"1.0\"?><plist version=\"1.0\"><array/></plist>",
            100.0,
        )
        .unwrap();
        assert!(missing.led_state().is_none());
        let mut snapshot = BatterySnapshot {
            adapter_voltage: 20.0,
            adapter_current: 3.0,
            pd_profiles: vec![PdProfile {
                voltage: 20.0,
                current: 4.8,
                watts: 96.0,
            }],
            ..Default::default()
        };
        assert_eq!(snapshot.adapter_power(), 60.0);
        snapshot.adapter_voltage = 0.0;
        assert_eq!(snapshot.adapter_power(), 96.0);
    }

    #[test]
    fn automatic_charger_baseline_matches_model_rules() {
        assert_eq!(
            baseline_from_product_tree("MacBook Pro 16-inch"),
            Some(140.0)
        );
        assert_eq!(
            baseline_from_product_tree(
                r#"MacBook Pro 14-inch "product-soc-name" = <"Apple M4 Pro">"#
            ),
            Some(96.0)
        );
        assert_eq!(
            baseline_from_product_tree("MacBook Pro 14-inch"),
            Some(70.0)
        );
        assert_eq!(baseline_from_product_tree("MacBook Air"), None);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn battery_query_timeout_terminates_the_child() {
        let error = bounded_command_output(
            std::process::Command::new("/bin/sleep").arg("2"),
            std::time::Duration::from_millis(50),
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }
}
