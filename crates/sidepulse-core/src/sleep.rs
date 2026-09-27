//! SidePulse power decisions without platform commands or UI state.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AwakePolicy {
    Never,
    Agents,
    Always,
}

impl AwakePolicy {
    pub fn should_hold(self, agents_active: bool) -> bool {
        match self {
            Self::Never => false,
            Self::Agents => agents_active,
            Self::Always => true,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BatteryPower {
    pub percent: f64,
    pub present: bool,
    pub plugged_in: bool,
}

pub fn battery_safeguard_active(battery: Option<BatteryPower>, threshold_percent: f64) -> bool {
    if !threshold_percent.is_finite() || threshold_percent <= 0.0 {
        return false;
    }
    let Some(battery) = battery else { return false };
    battery.present
        && !battery.plugged_in
        && battery.percent.is_finite()
        && battery.percent < threshold_percent.clamp(0.0, 100.0)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SleepInputs {
    pub policy: AwakePolicy,
    pub agents_active: Option<bool>,
    pub battery_safeguard_active: bool,
    pub lid_closed: Option<bool>,
    pub external_display_active: Option<bool>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SleepPlan {
    pub hold_caffeinate: bool,
    pub disable_system_sleep: bool,
    pub request_display_sleep: bool,
    pub hold_final_led_state: bool,
    pub run_lid_close_animation: bool,
}

pub fn plan_sleep(inputs: SleepInputs) -> SleepPlan {
    let awake_requested = inputs
        .policy
        .should_hold(inputs.agents_active.unwrap_or(false))
        && !inputs.battery_safeguard_active;
    let disable_system_sleep = awake_requested && inputs.external_display_active != Some(true);
    SleepPlan {
        hold_caffeinate: awake_requested,
        disable_system_sleep,
        request_display_sleep: inputs.lid_closed == Some(true)
            && inputs.external_display_active == Some(false)
            && disable_system_sleep,
        hold_final_led_state: inputs.lid_closed == Some(true)
            && inputs.agents_active.is_some()
            && !awake_requested,
        run_lid_close_animation: match inputs.policy {
            AwakePolicy::Never => true,
            AwakePolicy::Agents => inputs.agents_active == Some(false),
            AwakePolicy::Always => false,
        },
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct MacSleepSnapshot {
    pub sleep_disabled: Option<bool>,
    pub prevent_system_sleep: Option<bool>,
    pub prevent_user_idle_system_sleep: Option<bool>,
    pub prevent_user_idle_display_sleep: Option<bool>,
    pub user_is_active: Option<bool>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct PowerSnapshot {
    pub lid_closed: Option<bool>,
    pub external_display_active: Option<bool>,
    pub mac_sleep: MacSleepSnapshot,
}

impl MacSleepSnapshot {
    pub fn sleep_prevented(&self) -> Option<bool> {
        let known = [
            self.sleep_disabled,
            self.prevent_system_sleep,
            self.prevent_user_idle_system_sleep,
        ];
        if known.contains(&Some(true)) {
            Some(true)
        } else if known.contains(&None) {
            None
        } else {
            Some(false)
        }
    }
}

pub fn parse_ioreg_bool(text: &str, property_name: &str) -> Option<bool> {
    let key = format!("\"{property_name}\"");
    for line in text.lines() {
        let Some((_, value)) = line.split_once(&key) else {
            continue;
        };
        let Some(value) = value.trim_start().strip_prefix('=') else {
            continue;
        };
        match value
            .split_whitespace()
            .next()?
            .to_ascii_lowercase()
            .as_str()
        {
            "yes" | "true" | "1" => return Some(true),
            "no" | "false" | "0" => return Some(false),
            _ => {}
        }
    }
    None
}

pub fn parse_pmset_assertions(text: &str) -> HashMap<String, bool> {
    text.lines()
        .filter_map(|line| {
            let mut fields = line.split_whitespace();
            let name = fields.next()?;
            let value = fields.next()?;
            if fields.next().is_some()
                || !name.bytes().next()?.is_ascii_alphabetic()
                || !name.bytes().all(|byte| byte.is_ascii_alphanumeric())
            {
                return None;
            }
            Some((
                name.to_owned(),
                match value {
                    "1" => true,
                    "0" => false,
                    _ => return None,
                },
            ))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn inputs() -> SleepInputs {
        SleepInputs {
            policy: AwakePolicy::Agents,
            agents_active: Some(true),
            battery_safeguard_active: false,
            lid_closed: Some(true),
            external_display_active: Some(false),
        }
    }

    #[test]
    fn closed_lid_without_external_display_keeps_agent_running_and_sleeps_display() {
        let plan = plan_sleep(inputs());
        assert!(plan.hold_caffeinate);
        assert!(plan.disable_system_sleep);
        assert!(plan.request_display_sleep);
        assert!(!plan.hold_final_led_state);
    }

    #[test]
    fn external_or_unknown_display_never_gets_global_sleep_request() {
        for external_display_active in [Some(true), None] {
            let plan = plan_sleep(SleepInputs {
                external_display_active,
                ..inputs()
            });
            assert!(!plan.request_display_sleep);
        }
        assert!(
            !plan_sleep(SleepInputs {
                external_display_active: Some(true),
                ..inputs()
            })
            .disable_system_sleep
        );
    }

    #[test]
    fn idle_or_low_battery_releases_awake_request_and_holds_led_frame() {
        let idle = plan_sleep(SleepInputs {
            agents_active: Some(false),
            ..inputs()
        });
        assert!(!idle.hold_caffeinate);
        assert!(idle.hold_final_led_state);
        assert!(idle.run_lid_close_animation);
        let safeguard = plan_sleep(SleepInputs {
            battery_safeguard_active: true,
            ..inputs()
        });
        assert!(!safeguard.disable_system_sleep);
        assert!(safeguard.hold_final_led_state);
        assert!(battery_safeguard_active(
            Some(BatteryPower {
                percent: 19.0,
                present: true,
                plugged_in: false
            }),
            20.0
        ));
        assert!(!battery_safeguard_active(
            Some(BatteryPower {
                percent: 19.0,
                present: true,
                plugged_in: true
            }),
            20.0
        ));
    }

    #[test]
    fn mac_diagnostic_parsers_preserve_unknown_values() {
        assert_eq!(
            parse_ioreg_bool("| \"AppleClamshellState\" = Yes\n", "AppleClamshellState"),
            Some(true)
        );
        assert_eq!(
            parse_ioreg_bool("| \"SleepDisabled\" = No\n", "SleepDisabled"),
            Some(false)
        );
        assert_eq!(parse_ioreg_bool("unrelated", "SleepDisabled"), None);
        let assertions = parse_pmset_assertions(
            "PreventSystemSleep 1\nPreventUserIdleSystemSleep 0\ninvalid row extra\n",
        );
        assert!(assertions["PreventSystemSleep"]);
        assert!(!assertions["PreventUserIdleSystemSleep"]);
        let snapshot = MacSleepSnapshot {
            sleep_disabled: Some(false),
            prevent_system_sleep: Some(false),
            prevent_user_idle_system_sleep: None,
            ..Default::default()
        };
        assert_eq!(snapshot.sleep_prevented(), None);
    }
}
