//! Platform battery readers feed the common LED policy, independently of UI.

use std::io;

use crate::battery::BatteryState;

#[cfg(target_os = "macos")]
pub fn read_battery_state() -> io::Result<Option<BatteryState>> {
    use std::process::Command;

    let output = Command::new("pmset").args(["-g", "batt"]).output()?;
    if !output.status.success() {
        return Err(io::Error::other("pmset could not read battery status"));
    }
    Ok(parse_pmset(&String::from_utf8_lossy(&output.stdout)))
}

#[cfg(target_os = "macos")]
fn parse_pmset(text: &str) -> Option<BatteryState> {
    let line = text.lines().find(|line| line.contains('%'))?;
    let percent_start = line.find('%')?;
    let digits = line[..percent_start]
        .chars()
        .rev()
        .take_while(char::is_ascii_digit)
        .collect::<String>()
        .chars()
        .rev()
        .collect::<String>();
    let percent = digits.parse::<i32>().ok()?;
    let lower = line.to_ascii_lowercase();
    let plugged = text
        .lines()
        .next()
        .is_some_and(|first| first.contains("AC Power"));
    Some(BatteryState {
        percent,
        is_plugged: plugged,
        is_charging: lower.contains("charging")
            && !lower.contains("discharging")
            && !lower.contains("not charging"),
        is_charged: lower.contains("charged") || percent >= 100,
        ..Default::default()
    })
}

#[cfg(target_os = "linux")]
pub fn read_battery_state() -> io::Result<Option<BatteryState>> {
    read_linux_battery(std::path::Path::new("/sys/class/power_supply"))
}

#[cfg(target_os = "linux")]
fn read_linux_battery(root: &std::path::Path) -> io::Result<Option<BatteryState>> {
    let entries = match std::fs::read_dir(root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let mut batteries = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| {
            std::fs::read_to_string(path.join("type")).is_ok_and(|kind| kind.trim() == "Battery")
        })
        .collect::<Vec<_>>();
    batteries.sort();
    let Some(battery) = batteries.first() else {
        return Ok(None);
    };
    let percent = std::fs::read_to_string(battery.join("capacity"))?
        .trim()
        .parse::<i32>()
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    let status = std::fs::read_to_string(battery.join("status"))?.to_ascii_lowercase();
    let is_charging = status.trim() == "charging";
    let is_charged = status.trim() == "full" || percent >= 100;
    let mut is_plugged = is_charging || is_charged;
    if let Ok(entries) = std::fs::read_dir(root) {
        for supply in entries.filter_map(Result::ok) {
            let path = supply.path();
            if path == *battery {
                continue;
            }
            let kind = std::fs::read_to_string(path.join("type")).unwrap_or_default();
            if matches!(kind.trim(), "Mains" | "USB" | "USB_C")
                && std::fs::read_to_string(path.join("online"))
                    .is_ok_and(|online| online.trim() == "1")
            {
                is_plugged = true;
                break;
            }
        }
    }
    Ok(Some(BatteryState {
        percent,
        is_plugged,
        is_charging,
        is_charged,
        ..Default::default()
    }))
}

#[cfg(windows)]
pub fn read_battery_state() -> io::Result<Option<BatteryState>> {
    use windows_sys::Win32::System::Power::{GetSystemPowerStatus, SYSTEM_POWER_STATUS};

    let mut status = SYSTEM_POWER_STATUS::default();
    if unsafe { GetSystemPowerStatus(&mut status) } == 0 {
        return Err(io::Error::last_os_error());
    }
    if status.BatteryLifePercent == 255 || status.BatteryFlag & 128 != 0 {
        return Ok(None);
    }
    let percent = i32::from(status.BatteryLifePercent);
    Ok(Some(BatteryState {
        percent,
        is_plugged: status.ACLineStatus == 1,
        is_charging: status.BatteryFlag & 8 != 0,
        is_charged: percent >= 100,
        ..Default::default()
    }))
}

#[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
pub fn read_battery_state() -> io::Result<Option<BatteryState>> {
    Ok(None)
}

#[cfg(test)]
mod tests {
    #[cfg(target_os = "macos")]
    #[test]
    fn parses_mac_power_states() {
        use super::*;
        let charging = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=123)\t57%; charging; 1:30 remaining\n";
        let state = parse_pmset(charging).unwrap();
        assert_eq!(state.percent, 57);
        assert!(state.is_plugged && state.is_charging);
        let battery = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=123)\t80%; discharging; 3:00 remaining\n";
        let state = parse_pmset(battery).unwrap();
        assert!(!state.is_plugged && !state.is_charging);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn reads_linux_sysfs_battery_and_ac_state() {
        use super::*;
        use std::fs;
        let root = tempfile::tempdir().unwrap();
        let bat = root.path().join("BAT0");
        let ac = root.path().join("AC");
        fs::create_dir_all(&bat).unwrap();
        fs::create_dir_all(&ac).unwrap();
        fs::write(bat.join("type"), "Battery\n").unwrap();
        fs::write(bat.join("capacity"), "57\n").unwrap();
        fs::write(bat.join("status"), "Not charging\n").unwrap();
        fs::write(ac.join("type"), "Mains\n").unwrap();
        fs::write(ac.join("online"), "1\n").unwrap();
        let state = read_linux_battery(root.path()).unwrap().unwrap();
        assert_eq!(state.percent, 57);
        assert!(state.is_plugged);
        assert!(!state.is_charging);
    }
}
