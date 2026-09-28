mod battery;
mod delivery;
mod doctor;
mod helpers;
mod hook_config;
mod hook_impl;
mod leds;
mod lifecycle;
mod relay_link;
mod status;

pub use battery::run_battery;
pub use doctor::run_doctor;
pub use hook_config::run_hook_config;
pub use hook_impl::run_hook;
pub use relay_link::run_link;
pub use status::run_status;
pub use status::run_watch;

pub use delivery::{run_delivery, run_phone_link};

pub use helpers::{run_sd_guard, run_status_bar};

pub use leds::run_leds;
pub use lifecycle::{run_lifecycle, run_setup, run_upgrade};

pub fn preview_endpoint() -> Option<String> {
    std::env::var("SIDEPULSE_NEXT_ENDPOINT").ok().or_else(|| {
        let executable = std::env::current_exe().ok()?;
        sidepulse_installer::endpoint_from_executable(&executable)
            .ok()
            .flatten()
    })
}

pub fn run_reply(args: impl Iterator<Item = String>) -> std::process::ExitCode {
    let result = std::env::current_exe().and_then(|path| {
        std::process::Command::new(path.with_file_name(if cfg!(windows) {
            "sidepulse-next-reply.exe"
        } else {
            "sidepulse-next-reply"
        }))
        .args(args)
        .status()
    });
    match result {
        Ok(status) => std::process::ExitCode::from(status.code().unwrap_or(1).clamp(0, 255) as u8),
        Err(error) => {
            eprintln!("sidepulse-next: cannot start local classifier: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}
