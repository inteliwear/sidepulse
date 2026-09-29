use std::process::ExitCode;
fn main() -> ExitCode {
    let mut no_mount = false;
    let mut check = false;
    for arg in std::env::args().skip(1) {
        match arg.as_str() {
            "-n" | "--no-mount" => no_mount = true,
            "--check" => check = true,
            _ => {
                eprintln!("usage: sidepulse-next-sd-guard [-n|--no-mount] [--check]");
                return ExitCode::from(2);
            }
        }
    }
    let result = if check {
        sidepulse_helpers::check_sd_guard()
    } else {
        sidepulse_helpers::run_sd_guard(no_mount)
    };
    match result {
        Ok(()) => {
            if check {
                println!("SD eject protection is available.");
            }
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("sidepulse-next-sd-guard: {error}");
            ExitCode::FAILURE
        }
    }
}
