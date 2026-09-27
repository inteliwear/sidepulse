use std::io;

use sidepulse_core::PowerSnapshot;

#[cfg(target_os = "macos")]
pub struct MacPowerController {
    caffeinate: Option<std::process::Child>,
    system_disabled: bool,
    disable_attempted: bool,
    display_sleep_requested: bool,
}

#[cfg(target_os = "macos")]
impl MacPowerController {
    pub fn new() -> Self {
        Self {
            caffeinate: None,
            system_disabled: false,
            disable_attempted: false,
            display_sleep_requested: false,
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
                .is_some_and(|child| child.try_wait().ok().flatten().is_none());
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
        if should_disable && !self.system_disabled && !self.disable_attempted {
            self.disable_attempted = true;
            run_pmset_disablesleep(true)?;
            self.system_disabled = true;
            self.disable_attempted = false;
        } else if !should_disable && self.system_disabled {
            run_pmset_disablesleep(false)?;
            self.system_disabled = false;
            self.disable_attempted = false;
        } else if !should_disable {
            self.disable_attempted = false;
        }

        if plan.request_display_sleep && self.system_disabled && !self.display_sleep_requested {
            let status = Command::new("/usr/bin/pmset")
                .arg("displaysleepnow")
                .status()?;
            if !status.success() {
                return Err(io::Error::other("pmset displaysleepnow failed"));
            }
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
        if self.system_disabled {
            let _ = run_pmset_disablesleep(false);
        }
    }
}

#[cfg(target_os = "macos")]
fn run_pmset_disablesleep(enabled: bool) -> io::Result<()> {
    let status = std::process::Command::new("/usr/bin/sudo")
        .args([
            "-n",
            "/usr/bin/pmset",
            "-a",
            "disablesleep",
            if enabled { "1" } else { "0" },
        ])
        .status()?;
    if status.success() {
        Ok(())
    } else {
        Err(io::Error::other("closed-lid awake helper is unavailable"))
    }
}

#[cfg(target_os = "macos")]
pub fn observe() -> io::Result<PowerSnapshot> {
    use std::process::Command;

    use core_graphics::display::CGDisplay;
    use sidepulse_core::{MacSleepSnapshot, parse_ioreg_bool, parse_pmset_assertions};

    fn command_output(program: &str, args: &[&str]) -> Option<String> {
        let output = Command::new(program).args(args).output().ok()?;
        output
            .status
            .success()
            .then(|| String::from_utf8_lossy(&output.stdout).into_owned())
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
