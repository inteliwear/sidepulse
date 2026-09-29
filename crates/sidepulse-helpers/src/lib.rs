//! Platform helpers, independent from presentation and monitoring.
#[cfg(target_os = "macos")]
mod sd_guard;
pub mod sleep_helper;

/// Matches the existing DiskArbitration guard's hardware selection.
pub fn is_builtin_sd(protocol: Option<&str>, model: Option<&str>) -> bool {
    protocol.is_some_and(|protocol| protocol.contains("Secure Digital"))
        || model.is_some_and(|model| model.contains("SDXC"))
}
pub fn check_sd_guard() -> std::io::Result<()> {
    #[cfg(target_os = "macos")]
    {
        sd_guard::check()
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "SD eject protection is available only on macOS",
        ))
    }
}
pub fn run_sd_guard(no_mount: bool) -> std::io::Result<()> {
    #[cfg(target_os = "macos")]
    {
        sd_guard::run(no_mount)
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = no_mount;
        check_sd_guard()
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sd_hardware_selection_preserves_the_legacy_guard_boundary() {
        assert!(is_builtin_sd(Some("Secure Digital"), None));
        assert!(is_builtin_sd(
            Some("PCI-Express"),
            Some("Apple SDXC Reader")
        ));
        assert!(!is_builtin_sd(Some("USB"), Some("SidePulse Pro")));
        assert!(!is_builtin_sd(Some("SATA"), Some("External SSD")));
        assert!(!is_builtin_sd(None, None));
    }
}
