//! Portable battery-to-LED policy. Platform battery readers supply the snapshot.

use crate::apply_brightness;

pub const LOW_RED: &str = "#FF2600";
pub const MID_AMBER: &str = "#FFB000";
pub const HIGH_GREEN: &str = "#00FF66";
pub const CHARGING_MINT: &str = "#80FFC8";
pub const OFF: &str = "#000000";

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BatteryState {
    pub percent: i32,
    pub is_plugged: bool,
    pub is_charging: bool,
    pub is_charged: bool,
    pub adapter_watts: f64,
    pub full_charge_watts: f64,
}

impl Default for BatteryState {
    fn default() -> Self {
        Self {
            percent: 0,
            is_plugged: false,
            is_charging: false,
            is_charged: false,
            adapter_watts: 0.0,
            full_charge_watts: 100.0,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BatteryFill {
    pub full: usize,
    pub partial: f64,
    pub count: usize,
}

pub fn battery_fill(percent: i32, led_count: usize) -> BatteryFill {
    let count = led_count.clamp(1, 8);
    let raw = count as f64 * f64::from(percent.clamp(0, 100)) / 100.0;
    let full = (raw.floor() as usize).min(count);
    BatteryFill {
        full,
        partial: if full == count {
            0.0
        } else {
            raw - full as f64
        },
        count,
    }
}

fn color_for_percent(percent: i32) -> &'static str {
    if percent <= 20 {
        LOW_RED
    } else if percent <= 50 {
        MID_AMBER
    } else {
        HIGH_GREEN
    }
}

fn scale_hex_color(color: &str, ratio: f64) -> String {
    let scaled = (1..6)
        .step_by(2)
        .map(|index| {
            let channel = u8::from_str_radix(&color[index..index + 2], 16).unwrap();
            (f64::from(channel) * ratio.clamp(0.0, 1.0)).round() as u8
        })
        .collect::<Vec<_>>();
    format!("#{:02X}{:02X}{:02X}", scaled[0], scaled[1], scaled[2])
}

fn fill_color(index: usize, fill: BatteryFill, color: &str) -> String {
    if index < fill.full {
        color.to_owned()
    } else if index == fill.full && fill.partial > 0.0 {
        scale_hex_color(color, fill.partial)
    } else {
        OFF.to_owned()
    }
}

fn segment(index: usize, color: &str, transition_ms: u32) -> String {
    if transition_ms == 0 {
        format!("{index}:{color}")
    } else {
        format!("{index}:{color} {transition_ms}ms ease")
    }
}

pub fn program_for_battery(
    state: BatteryState,
    led_count: usize,
    transition_ms: u32,
    brightness: u8,
) -> String {
    let fill = battery_fill(state.percent, led_count);
    let percent = state.percent.clamp(0, 100);
    let static_display =
        !state.is_plugged || !state.is_charging || state.is_charged || percent >= 100;
    if static_display {
        let color = color_for_percent(percent);
        let segments = (0..fill.count)
            .map(|index| {
                let target = if state.is_charged || percent >= 100 {
                    HIGH_GREEN.to_owned()
                } else {
                    fill_color(index, fill, color)
                };
                segment(index, &target, transition_ms)
            })
            .collect::<Vec<_>>()
            .join(";");
        return apply_brightness(&segments, brightness);
    }

    let ratio = if state.full_charge_watts > 0.0 {
        (state.adapter_watts / state.full_charge_watts).clamp(0.0, 1.0)
    } else {
        0.0
    };
    let pulse_ms = (180.0 + 1220.0 * ratio).round() as u32;
    let cycle_ms = ((3.0 - 2.1 * ratio).max(0.9) * 1000.0).round() as u32;
    let hold_ms = cycle_ms.saturating_sub(transition_ms.saturating_add(pulse_ms));
    let pulse_index = fill.full.min(fill.count - 1);
    let mut lines = vec![
        (0..fill.count)
            .map(|index| {
                segment(
                    index,
                    &fill_color(index, fill, CHARGING_MINT),
                    transition_ms,
                )
            })
            .collect::<Vec<_>>()
            .join(";"),
        format!("{pulse_index}:{CHARGING_MINT} {pulse_ms}ms pulse"),
    ];
    if hold_ms > 0 {
        lines.push(format!(
            "{pulse_index}:{} {hold_ms}ms none",
            fill_color(pulse_index, fill, CHARGING_MINT)
        ));
    }
    lines.push("repeat".to_owned());
    apply_brightness(&lines.join("\n"), brightness)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::validate_led_text;

    #[test]
    fn charging_frontier_matches_legacy_program() {
        let state = BatteryState {
            percent: 50,
            is_plugged: true,
            is_charging: true,
            adapter_watts: 70.0,
            full_charge_watts: 140.0,
            ..Default::default()
        };
        let program = program_for_battery(state, 8, 360, 255);
        assert!(program.contains("3:#80FFC8 360ms ease;4:#000000 360ms ease"));
        assert!(program.contains("\n4:#80FFC8 790ms pulse\n4:#000000 800ms none\nrepeat"));
        validate_led_text(&program).unwrap();
    }

    #[test]
    fn partial_and_full_battery_match_legacy_colors() {
        let state = BatteryState {
            percent: 57,
            ..Default::default()
        };
        let program = program_for_battery(state, 8, 360, 128);
        assert!(program.starts_with("brightness 128\n"));
        assert!(program.contains("4:#008F39 360ms ease;5:#000000 360ms ease"));
        assert!(!program.contains("repeat"));
        let charged = BatteryState {
            percent: 80,
            is_charged: true,
            ..Default::default()
        };
        assert_eq!(
            program_for_battery(charged, 2, 0, 255),
            "0:#00FF66;1:#00FF66"
        );
    }
}
