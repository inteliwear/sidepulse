//! Dedicated development hook entry point.

fn main() {
    let _ = sidepulse_cli::run_hook(std::env::args().skip(1));
}
