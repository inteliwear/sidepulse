//! Explicit installation of the existing narrowly scoped macOS pmset rule.
//! Planning never invokes sudo or changes power state.
use serde::Serialize;
#[cfg(any(target_os = "macos", test))]
use std::io::Write;
use std::{
    fs,
    io::{self, Read},
    path::{Path, PathBuf},
};
pub const DEFAULT_PATH: &str = "/etc/sudoers.d/sidepulse-disablesleep";
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Operation {
    Install,
    Remove,
}
#[derive(Debug, Clone, Serialize)]
pub struct Plan {
    pub path: PathBuf,
    pub user: String,
    pub operation: Operation,
    pub changed: bool,
    pub installed: bool,
    pub rule: String,
    #[cfg(any(target_os = "macos", test))]
    #[serde(skip)]
    original: Option<Vec<u8>>,
}
fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
pub fn rule_for_user(user: &str) -> io::Result<String> {
    if user.is_empty()
        || !user
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
    {
        return Err(invalid("invalid sudoers user"));
    }
    Ok(format!(
        "{user} ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 0, /usr/bin/pmset -a disablesleep 1\n"
    ))
}
fn read(path: &Path) -> io::Result<Option<Vec<u8>>> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if !metadata.is_file() => {
            return Err(invalid("sleep helper must be a regular file"));
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    }
    let mut bytes = Vec::new();
    fs::File::open(path)?.take(8193).read_to_end(&mut bytes)?;
    if bytes.len() > 8192 {
        return Err(invalid("sleep helper rule is too large"));
    }
    Ok(Some(bytes))
}
fn is_owned_rule(bytes: &[u8]) -> bool {
    let Ok(text) = std::str::from_utf8(bytes) else {
        return false;
    };
    let Some((user, _)) = text.split_once(' ') else {
        return false;
    };
    rule_for_user(user).is_ok_and(|rule| rule.as_bytes() == bytes)
}
pub fn plan(path: &Path, user: &str, operation: Operation) -> io::Result<Plan> {
    let rule = rule_for_user(user)?;
    let original = read(path)?;
    if original.as_ref().is_some_and(|bytes| !is_owned_rule(bytes)) {
        return Err(invalid(
            "existing file contains another rule; refusing to replace or remove it",
        ));
    }
    let installed = original.as_deref() == Some(rule.as_bytes());
    let changed = match operation {
        Operation::Install => !installed,
        Operation::Remove => original.is_some(),
    };
    Ok(Plan {
        path: path.into(),
        user: user.into(),
        operation,
        changed,
        installed,
        rule,
        #[cfg(any(target_os = "macos", test))]
        original,
    })
}
#[cfg(target_os = "macos")]
pub fn apply(plan: &Plan) -> io::Result<()> {
    if unsafe { libc::geteuid() } != 0 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "run the explicit helper installation with sudo",
        ));
    }
    apply_validated(plan, |file| {
        use std::os::fd::AsRawFd;
        use std::os::unix::fs::PermissionsExt;
        file.as_file()
            .set_permissions(fs::Permissions::from_mode(0o440))?;
        if unsafe { libc::fchown(file.as_file().as_raw_fd(), 0, 0) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let output = std::process::Command::new("/usr/sbin/visudo")
            .arg("-cf")
            .arg(file.path())
            .output()?;
        if !output.status.success() {
            return Err(io::Error::other(format!(
                "sudoers validation failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            )));
        }
        Ok(())
    })
}
#[cfg(not(target_os = "macos"))]
pub fn apply(_plan: &Plan) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "the closed-lid helper is available only on macOS",
    ))
}
#[cfg(any(target_os = "macos", test))]
fn apply_validated(
    plan: &Plan,
    validate: impl FnOnce(&tempfile::NamedTempFile) -> io::Result<()>,
) -> io::Result<()> {
    if read(&plan.path)? != plan.original {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "sleep helper changed since planning",
        ));
    }
    if !plan.changed {
        return Ok(());
    }
    match plan.operation {
        Operation::Remove => fs::remove_file(&plan.path),
        Operation::Install => {
            let parent = plan
                .path
                .parent()
                .ok_or_else(|| invalid("sleep helper needs a parent directory"))?;
            fs::create_dir_all(parent)?;
            let mut file = tempfile::NamedTempFile::new_in(parent)?;
            file.write_all(plan.rule.as_bytes())?;
            file.as_file().sync_all()?;
            validate(&file)?;
            if read(&plan.path)? != plan.original {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "sleep helper changed during validation",
                ));
            }
            file.persist(&plan.path).map_err(|error| error.error)?;
            Ok(())
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_the_legacy_pmset_rule_can_be_installed_or_removed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rule");
        assert!(rule_for_user("user ALL=(ALL)").is_err());
        assert!(rule_for_user("user\nroot").is_err());
        let planned = plan(&path, "test-user", Operation::Install).unwrap();
        assert!(planned.changed);
        let failure = apply_validated(&planned, |_| Err(io::Error::other("invalid sudoers")));
        assert!(failure.is_err());
        assert!(!path.exists());
        apply_validated(&planned, |_| Ok(())).unwrap();
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            rule_for_user("test-user").unwrap()
        );
        assert!(
            !plan(&path, "test-user", Operation::Install)
                .unwrap()
                .changed
        );
        let remove = plan(&path, "test-user", Operation::Remove).unwrap();
        fs::write(&path, "external").unwrap();
        assert_eq!(
            apply_validated(&remove, |_| Ok(())).unwrap_err().kind(),
            io::ErrorKind::AlreadyExists
        );
        assert_eq!(fs::read_to_string(&path).unwrap(), "external");
        assert!(plan(&path, "test-user", Operation::Remove).is_err());
    }
    #[cfg(unix)]
    #[test]
    fn a_rule_symlink_is_not_followed_or_replaced() {
        let dir = tempfile::tempdir().unwrap();
        let original = dir.path().join("original");
        fs::write(&original, rule_for_user("test").unwrap()).unwrap();
        let path = dir.path().join("link");
        std::os::unix::fs::symlink(&original, &path).unwrap();
        assert!(plan(&path, "test", Operation::Install).is_err());
    }
}
