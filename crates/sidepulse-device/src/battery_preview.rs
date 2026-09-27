//! Plug/unplug preview state shared by service output adapters.

use std::time::{Duration, Instant};

use crate::battery::BatteryState;

#[derive(Default)]
pub struct BatteryPreview {
    pub latest: Option<BatteryState>,
    last_plugged: Option<bool>,
    until: Option<Instant>,
}

impl BatteryPreview {
    pub fn observe(
        &mut self,
        battery: Option<BatteryState>,
        enabled: bool,
        seconds: f64,
        now: Instant,
    ) {
        self.latest = battery;
        let Some(battery) = battery else {
            return;
        };
        if self
            .last_plugged
            .is_some_and(|plugged| plugged != battery.is_plugged)
            && enabled
        {
            self.until = Duration::try_from_secs_f64(seconds)
                .ok()
                .and_then(|duration| now.checked_add(duration));
        }
        self.last_plugged = Some(battery.is_plugged);
    }

    pub fn display<'a>(&self, configured: &'a str, now: Instant) -> &'a str {
        match configured {
            "custom" => "custom",
            "battery" => "battery",
            _ if self.latest.is_some() && self.until.is_some_and(|until| now < until) => "battery",
            _ => "agent",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn previews_transitions_then_returns_to_agents_and_preserves_manual_output() {
        let now = Instant::now();
        let mut preview = BatteryPreview::default();
        preview.observe(Some(BatteryState::default()), true, 7.0, now);
        assert_eq!(preview.display("agent", now), "agent");
        let plugged = BatteryState {
            is_plugged: true,
            ..Default::default()
        };
        preview.observe(Some(plugged), true, 7.0, now);
        assert_eq!(preview.display("agent", now), "battery");
        assert_eq!(preview.display("custom", now), "custom");
        preview.observe(Some(plugged), true, 7.0, now + Duration::from_secs(6));
        assert_eq!(
            preview.display("agent", now + Duration::from_secs(7)),
            "agent"
        );
        preview.observe(
            Some(BatteryState::default()),
            false,
            7.0,
            now + Duration::from_secs(8),
        );
        assert_eq!(
            preview.display("agent", now + Duration::from_secs(8)),
            "agent"
        );
        assert_eq!(
            preview.display("battery", now + Duration::from_secs(8)),
            "battery"
        );
    }
}
