//! Lid transition timing and output holding, independent of device I/O.

use crate::{SleepInputs, plan_sleep};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LidOutputAction {
    Live,
    Transition { state: &'static str },
    Hold,
}

#[derive(Debug, Clone, Default)]
pub struct LidOutputPolicy {
    last_lid: Option<bool>,
    transition: Option<(&'static str, u64)>,
}
impl LidOutputPolicy {
    pub fn action(
        &mut self,
        mut inputs: SleepInputs,
        now_ms: u64,
        open_ms: u64,
        close_ms: u64,
    ) -> LidOutputAction {
        if let Some(closed) = inputs.lid_closed {
            if self.last_lid.is_some_and(|previous| previous != closed) {
                let plan = plan_sleep(inputs);
                self.transition = if closed && !plan.run_lid_close_animation {
                    None
                } else {
                    Some((
                        if closed { "lid_closed" } else { "lid_open" },
                        now_ms
                            .saturating_add(if closed { close_ms } else { open_ms })
                            .saturating_add(150),
                    ))
                };
            }
            self.last_lid = Some(closed);
        }
        // A failed observation cannot create an edge or release a held frame.
        inputs.lid_closed = self.last_lid;
        if let Some((state, until)) = self.transition {
            if now_ms < until {
                return LidOutputAction::Transition { state };
            }
            self.transition = None;
        }
        if plan_sleep(inputs).hold_final_led_state {
            LidOutputAction::Hold
        } else {
            LidOutputAction::Live
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::AwakePolicy;
    fn inputs(lid_closed: Option<bool>, active: bool) -> SleepInputs {
        SleepInputs {
            policy: AwakePolicy::Agents,
            agents_active: Some(active),
            battery_safeguard_active: false,
            lid_closed,
            external_display_active: Some(false),
        }
    }
    #[test]
    fn closes_then_holds_and_releases_when_work_resumes() {
        let mut output = LidOutputPolicy::default();
        assert_eq!(
            output.action(inputs(Some(false), false), 0, 1000, 1300),
            LidOutputAction::Live
        );
        assert_eq!(
            output.action(inputs(Some(true), false), 100, 1000, 1300),
            LidOutputAction::Transition {
                state: "lid_closed"
            }
        );
        assert_eq!(
            output.action(inputs(None, false), 1500, 1000, 1300),
            LidOutputAction::Transition {
                state: "lid_closed"
            }
        );
        assert_eq!(
            output.action(inputs(Some(true), false), 1700, 1000, 1300),
            LidOutputAction::Hold
        );
        assert_eq!(
            output.action(inputs(Some(true), true), 1800, 1000, 1300),
            LidOutputAction::Live
        );
        assert_eq!(
            output.action(inputs(Some(true), false), 1900, 1000, 1300),
            LidOutputAction::Hold
        );
    }
    #[test]
    fn opening_interrupts_close_and_awake_closing_skips_animation() {
        let mut output = LidOutputPolicy::default();
        assert_eq!(
            output.action(inputs(Some(false), true), 0, 1000, 1300),
            LidOutputAction::Live
        );
        assert_eq!(
            output.action(inputs(Some(true), true), 10, 1000, 1300),
            LidOutputAction::Live
        );
        assert_eq!(
            output.action(inputs(Some(false), true), 20, 1000, 1300),
            LidOutputAction::Transition { state: "lid_open" }
        );
        assert_eq!(
            output.action(inputs(Some(true), false), 30, 1000, 1300),
            LidOutputAction::Transition {
                state: "lid_closed"
            }
        );
        assert_eq!(
            output.action(inputs(Some(false), false), 40, 1000, 1300),
            LidOutputAction::Transition { state: "lid_open" }
        );
        assert_eq!(
            output.action(inputs(Some(false), false), 1290, 1000, 1300),
            LidOutputAction::Live
        );
    }
    #[test]
    fn startup_closed_holds_existing_frame_without_inventing_an_edge() {
        let mut output = LidOutputPolicy::default();
        assert_eq!(
            output.action(inputs(Some(true), false), 0, 1000, 1300),
            LidOutputAction::Hold
        );
        assert_eq!(
            output.action(inputs(None, false), 1000, 1000, 1300),
            LidOutputAction::Hold
        );
    }
}
