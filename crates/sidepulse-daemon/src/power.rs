use std::io;

use sidepulse_core::PowerSnapshot;

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
