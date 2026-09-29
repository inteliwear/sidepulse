//! Native drive discovery. Volume queries return errors without prompting for
//! missing media, and restore the calling thread's previous error mode.

use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::ptr;

use windows_sys::Win32::Storage::FileSystem::{
    GetDriveTypeW, GetLogicalDrives, GetVolumeInformationW,
};
use windows_sys::Win32::System::Diagnostics::Debug::{
    GetThreadErrorMode, SEM_FAILCRITICALERRORS, SEM_NOOPENFILEERRORBOX, SetThreadErrorMode,
};
use windows_sys::Win32::System::WindowsProgramming::{DRIVE_FIXED, DRIVE_REMOVABLE};

struct ErrorModeGuard(u32);

impl ErrorModeGuard {
    fn new() -> Option<Self> {
        let previous = unsafe { GetThreadErrorMode() };
        let success = unsafe {
            SetThreadErrorMode(
                previous | SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX,
                ptr::null_mut(),
            )
        };
        if success != 0 {
            Some(Self(previous))
        } else {
            None
        }
    }
}

impl Drop for ErrorModeGuard {
    fn drop(&mut self) {
        unsafe { SetThreadErrorMode(self.0, ptr::null_mut()) };
    }
}

pub(crate) fn is_drive_root(path: &Path) -> bool {
    let text = path.as_os_str().to_string_lossy();
    let bytes = text.as_bytes();
    bytes.len() == 3
        && bytes[0].is_ascii_alphabetic()
        && bytes[1] == b':'
        && matches!(bytes[2], b'\\' | b'/')
}

fn roots_from_mask(mask: u32) -> Vec<PathBuf> {
    (0..26)
        .filter(|index| mask & (1 << index) != 0)
        .map(|index| PathBuf::from(format!("{}:\\", char::from(b'A' + index as u8))))
        .collect()
}

pub(crate) fn mounted_drive_roots() -> Vec<PathBuf> {
    let Some(_guard) = ErrorModeGuard::new() else {
        return Vec::new();
    };
    roots_from_mask(unsafe { GetLogicalDrives() })
        .into_iter()
        .filter(|root| {
            let wide = wide_path(root);
            matches!(
                unsafe { GetDriveTypeW(wide.as_ptr()) },
                DRIVE_FIXED | DRIVE_REMOVABLE
            )
        })
        .collect()
}

pub(crate) fn volume_label(root: &Path) -> Option<String> {
    if !is_drive_root(root) {
        return None;
    }
    let _guard = ErrorModeGuard::new()?;
    let wide = wide_path(root);
    let mut label = [0_u16; 261];
    let success = unsafe {
        GetVolumeInformationW(
            wide.as_ptr(),
            label.as_mut_ptr(),
            label.len() as u32,
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
            0,
        )
    };
    if success == 0 {
        return None;
    }
    let length = label
        .iter()
        .position(|value| *value == 0)
        .unwrap_or(label.len());
    Some(String::from_utf16_lossy(&label[..length]))
}

fn wide_path(path: &Path) -> Vec<u16> {
    path.as_os_str().encode_wide().chain(Some(0)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn drive_mask_and_root_validation_are_bounded() {
        assert_eq!(
            roots_from_mask((1 << 2) | (1 << 25)),
            [PathBuf::from("C:\\"), PathBuf::from("Z:\\")]
        );
        assert!(is_drive_root(Path::new("D:\\")));
        assert!(!is_drive_root(Path::new("D:\\folder")));
        assert!(!is_drive_root(Path::new("\\\\server\\share\\")));
    }

    #[test]
    fn native_probe_restores_thread_error_mode() {
        let previous = unsafe { GetThreadErrorMode() };
        let roots = mounted_drive_roots();
        assert!(
            !roots.is_empty(),
            "Windows must expose its local system drive"
        );
        let mut readable_volume = false;
        for root in roots {
            assert!(is_drive_root(&root));
            readable_volume |= volume_label(&root).is_some();
        }
        assert!(
            readable_volume,
            "native volume query must read at least one local drive"
        );
        assert_eq!(unsafe { GetThreadErrorMode() }, previous);
    }
}
