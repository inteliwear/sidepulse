use std::io;

use sidepulse_core::PowerSnapshot;

#[cfg(target_os = "macos")]
pub struct MacPowerController {
    caffeinate: Option<std::process::Child>,
    system_disabled: bool,
    helper_error: Option<String>,
    retry_after: Option<std::time::Instant>,
    display_sleep_requested: bool,
}

#[cfg(target_os = "macos")]
impl MacPowerController {
    pub fn new() -> Self {
        Self {
            caffeinate: None,
            system_disabled: false,
            helper_error: None,
            retry_after: None,
            display_sleep_requested: false,
        }
    }

    pub fn active(&mut self) -> bool {
        self.caffeinate
            .as_mut()
            .is_some_and(|child| child.try_wait().is_ok_and(|status| status.is_none()))
    }

    pub fn system_sleep_disabled(&self) -> bool {
        self.system_disabled
    }

    pub fn retry_helper(&mut self) {
        self.retry_after = None;
    }
    fn set_system_sleep(&mut self, enabled: bool) -> io::Result<()> {
        self.set_system_sleep_with(enabled, std::time::Instant::now(), run_pmset_disablesleep)
    }
    fn set_system_sleep_with(
        &mut self,
        enabled: bool,
        now: std::time::Instant,
        runner: impl FnOnce(bool) -> io::Result<()>,
    ) -> io::Result<()> {
        if self.retry_after.is_some_and(|at| now < at) {
            return Err(io::Error::other(
                self.helper_error
                    .as_deref()
                    .unwrap_or("closed-lid awake helper is unavailable"),
            ));
        }
        match runner(enabled) {
            Ok(()) => {
                self.system_disabled = enabled;
                self.helper_error = None;
                self.retry_after = None;
                Ok(())
            }
            Err(error) => {
                self.helper_error = Some(error.to_string());
                self.retry_after =
                    Some(std::time::Instant::now() + std::time::Duration::from_secs(30));
                Err(error)
            }
        }
    }
    pub fn sync(
        &mut self,
        plan: sidepulse_core::SleepPlan,
        allow_system_override: bool,
    ) -> io::Result<()> {
        use std::process::{Command, Stdio};

        if plan.hold_caffeinate {
            let running = self
                .caffeinate
                .as_mut()
                .is_some_and(|child| child.try_wait().is_ok_and(|status| status.is_none()));
            if !running {
                self.caffeinate = Some(
                    Command::new("/usr/bin/caffeinate")
                        .args(["-ims", "-w", &std::process::id().to_string()])
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .spawn()?,
                );
            }
        } else if let Some(mut child) = self.caffeinate.take() {
            let _ = child.kill();
            let _ = child.wait();
        }

        let should_disable = allow_system_override && plan.disable_system_sleep;
        if should_disable != self.system_disabled {
            self.set_system_sleep(should_disable)?;
        } else {
            self.helper_error = None;
            self.retry_after = None;
        }

        if plan.request_display_sleep && self.system_disabled && !self.display_sleep_requested {
            sidepulse_device::battery_diagnostics::bounded_command_output(
                Command::new("/usr/bin/pmset").arg("displaysleepnow"),
                std::time::Duration::from_secs(3),
            )?;
            self.display_sleep_requested = true;
        } else if !plan.request_display_sleep {
            self.display_sleep_requested = false;
        }
        Ok(())
    }
}

#[cfg(target_os = "macos")]
impl Drop for MacPowerController {
    fn drop(&mut self) {
        if let Some(mut child) = self.caffeinate.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        #[cfg(not(test))]
        if self.system_disabled
            && let Err(error) = run_pmset_disablesleep(false)
        {
            eprintln!(
                "sidepulse-next-service: could not restore system sleep during shutdown: {error}"
            );
        }
    }
}

#[cfg(target_os = "macos")]
fn run_pmset_disablesleep(enabled: bool) -> io::Result<()> {
    sidepulse_device::battery_diagnostics::bounded_command_output(
        std::process::Command::new("/usr/bin/sudo").args([
            "-n",
            "/usr/bin/pmset",
            "-a",
            "disablesleep",
            if enabled { "1" } else { "0" },
        ]),
        std::time::Duration::from_secs(3),
    )
    .map(|_| ())
    .map_err(|error| io::Error::new(error.kind(), format!("closed-lid awake helper: {error}")))
}

#[cfg(target_os = "macos")]
pub fn observe() -> io::Result<PowerSnapshot> {
    use std::process::Command;

    use core_graphics::display::CGDisplay;
    use sidepulse_core::{MacSleepSnapshot, parse_ioreg_bool, parse_pmset_assertions};

    fn command_output(program: &str, args: &[&str]) -> Option<String> {
        let output = sidepulse_device::battery_diagnostics::bounded_command_output(
            Command::new(program).args(args),
            std::time::Duration::from_secs(2),
        )
        .ok()?;
        Some(String::from_utf8_lossy(&output).into_owned())
    }

    let lid_closed = command_output(
        "/usr/sbin/ioreg",
        &["-r", "-k", "AppleClamshellState", "-d", "4"],
    )
    .and_then(|text| parse_ioreg_bool(&text, "AppleClamshellState"));
    let external_display_active = CGDisplay::active_displays().ok().map(|ids| {
        ids.into_iter().any(|id| {
            let display = CGDisplay::new(id);
            !display.is_builtin() && display.is_active()
        })
    });
    let sleep_disabled =
        command_output("/usr/sbin/ioreg", &["-r", "-k", "SleepDisabled", "-d", "4"])
            .and_then(|text| parse_ioreg_bool(&text, "SleepDisabled"));
    let assertions = command_output("/usr/bin/pmset", &["-g", "assertions"])
        .map(|text| parse_pmset_assertions(&text))
        .unwrap_or_default();
    Ok(PowerSnapshot {
        lid_closed,
        external_display_active,
        mac_sleep: MacSleepSnapshot {
            sleep_disabled,
            prevent_system_sleep: assertions.get("PreventSystemSleep").copied(),
            prevent_user_idle_system_sleep: assertions.get("PreventUserIdleSystemSleep").copied(),
            prevent_user_idle_display_sleep: assertions.get("PreventUserIdleDisplaySleep").copied(),
            user_is_active: assertions.get("UserIsActive").copied(),
        },
    })
}

#[cfg(not(target_os = "macos"))]
pub fn observe() -> io::Result<PowerSnapshot> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "macOS power status is unavailable on this platform",
    ))
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    #[test]
    fn missing_helper_stays_visible_and_recovers_after_retry_without_power_commands() {
        let mut controller = MacPowerController::new();
        let now = std::time::Instant::now();
        assert!(
            controller
                .set_system_sleep_with(true, now, |_| Err(io::Error::other("missing helper")))
                .is_err()
        );
        assert!(
            controller
                .set_system_sleep_with(true, now + std::time::Duration::from_secs(1), |_| panic!(
                    "must back off"
                ))
                .is_err()
        );
        assert!(!controller.system_sleep_disabled());
        controller.retry_helper();
        controller
            .set_system_sleep_with(true, now + std::time::Duration::from_secs(2), |enabled| {
                assert!(enabled);
                Ok(())
            })
            .unwrap();
        assert!(controller.system_sleep_disabled());
        assert!(
            controller
                .set_system_sleep_with(false, now + std::time::Duration::from_secs(3), |_| Err(
                    io::Error::other("restore failed")
                ))
                .is_err()
        );
        assert!(controller.system_sleep_disabled());
        controller.retry_helper();
        controller
            .set_system_sleep_with(false, now + std::time::Duration::from_secs(4), |enabled| {
                assert!(!enabled);
                Ok(())
            })
            .unwrap();
        assert!(!controller.system_sleep_disabled());
        assert!(controller.helper_error.is_none());
    }
}
