//! Relocatable native payloads. Checksums detect corruption; OS signatures
//! authenticate distribution files independently of this manifest.
use crate::{
    Platform, StagePlan,
    upgrade::{bounded_read, files, hash_paths},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{self, Read, Write},
    path::{Path, PathBuf},
};
const MANIFEST: &str = "package.json";
#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct PackageFile {
    pub path: String,
    pub bytes: u64,
    pub sha256: String,
    pub executable: bool,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct PackageManifest {
    pub schema_version: u8,
    pub version: String,
    pub platform: Platform,
    pub architecture: String,
    pub files: Vec<PackageFile>,
}
fn fail(message: &str) -> io::Error {
    io::Error::other(message)
}
fn file_hash(path: &Path) -> io::Result<String> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_file() || meta.len() > 512 * 1024 * 1024 {
        return Err(fail("package file is not bounded and regular"));
    }
    let mut digest = Sha256::new();
    let mut input = fs::File::open(path)?.take(meta.len() + 1);
    let mut buffer = [0u8; 65536];
    let mut length = 0;
    loop {
        let n = input.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        length += n as u64;
        digest.update(&buffer[..n]);
    }
    if length != meta.len() {
        return Err(fail("package changed while reading"));
    }
    Ok(format!("{:x}", digest.finalize()))
}
fn entries(root: &Path) -> io::Result<Vec<PackageFile>> {
    let mut paths = Vec::new();
    files(root, Path::new(""), &mut paths)?;
    paths.sort();
    let mut result = Vec::new();
    let mut total = 0;
    for path in paths {
        if path == Path::new(MANIFEST) {
            continue;
        }
        let meta = fs::symlink_metadata(root.join(&path))?;
        total += meta.len();
        if total > 512 * 1024 * 1024 {
            return Err(fail("package exceeds 512 MiB"));
        }
        #[cfg(unix)]
        let executable = {
            use std::os::unix::fs::PermissionsExt;
            meta.permissions().mode() & 0o111 != 0
        };
        #[cfg(not(unix))]
        let executable = path.extension().is_some_and(|extension| extension == "exe");
        result.push(PackageFile {
            path: path
                .to_str()
                .ok_or_else(|| fail("package path is not Unicode"))?
                .replace('\\', "/"),
            bytes: meta.len(),
            sha256: file_hash(&root.join(path))?,
            executable,
        });
    }
    Ok(result)
}
pub(crate) fn copy_tree(source: &Path, target: &Path) -> io::Result<()> {
    let mut paths = Vec::new();
    files(source, Path::new(""), &mut paths)?;
    let mut total = 0;
    fs::create_dir_all(target)?;
    for path in paths {
        total += fs::symlink_metadata(source.join(&path))?.len();
        if total > 512 * 1024 * 1024 {
            return Err(fail("native application exceeds 512 MiB"));
        }
        let destination = target.join(&path);
        fs::create_dir_all(destination.parent().unwrap())?;
        fs::copy(source.join(path), destination)?;
    }
    Ok(())
}
pub(crate) fn tree_hash(root: &Path) -> io::Result<String> {
    let mut paths = Vec::new();
    files(root, Path::new(""), &mut paths)?;
    paths.sort();
    hash_paths(root, &paths)
}
pub(crate) fn application_source(source: &Path, name: &std::ffi::OsStr) -> Option<PathBuf> {
    let root = if source.parent()?.join(MANIFEST).is_file()
        || source.parent()?.join("manifest.json").is_file()
    {
        source.parent()?
    } else {
        source
    };
    let app = root.join("applications").join(name);
    app.exists().then_some(app)
}
pub(crate) fn resolve_source(source: &Path) -> io::Result<PathBuf> {
    let source = fs::canonicalize(source)?;
    if source.join(MANIFEST).exists() {
        verify(&source)?;
        return Ok(source.join("bin"));
    }
    verify_source(&source)?;
    Ok(source)
}
pub(crate) fn verify_source(source: &Path) -> io::Result<()> {
    if let Some(parent) = source
        .parent()
        .filter(|parent| parent.join(MANIFEST).exists())
    {
        verify(parent)?;
    }
    if let Some(parent) = source
        .parent()
        .filter(|parent| parent.join("manifest.json").exists())
    {
        crate::read_stage(parent)?;
    }
    Ok(())
}
/// Seal after platform signing and stapling, so the manifest hashes final bytes.
/// This operation does not assert that an OS signature is present or trusted.
pub fn seal(root: &Path, version: &str) -> io::Result<PackageManifest> {
    if version.is_empty()
        || version.len() > 64
        || !version
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".-+".contains(&b))
    {
        return Err(fail("package version must be a short release identifier"));
    }
    let manifest = PackageManifest {
        schema_version: 1,
        version: version.into(),
        platform: Platform::current()?,
        architecture: std::env::consts::ARCH.into(),
        files: entries(root)?,
    };
    let mut temporary = tempfile::NamedTempFile::new_in(root)?;
    serde_json::to_writer_pretty(&mut temporary, &manifest)?;
    temporary.write_all(b"\n")?;
    temporary.as_file().sync_all()?;
    temporary
        .persist(root.join(MANIFEST))
        .map_err(|error| error.error)?;
    Ok(manifest)
}
pub fn verify(root: &Path) -> io::Result<PackageManifest> {
    let manifest: PackageManifest =
        serde_json::from_slice(&bounded_read(&root.join(MANIFEST), 4 * 1024 * 1024)?)?;
    if manifest.schema_version != 1
        || manifest.platform != Platform::current()?
        || manifest.architecture != std::env::consts::ARCH
    {
        return Err(fail(
            "native package is for a different platform, architecture, or schema",
        ));
    }
    if entries(root)? != manifest.files {
        return Err(fail("native package checksums or file list differ"));
    }
    for name in crate::BINARIES {
        if !root
            .join("bin")
            .join(format!("{name}{}", std::env::consts::EXE_SUFFIX))
            .is_file()
        {
            return Err(fail("native package is missing a required binary"));
        }
    }
    if manifest.platform == Platform::Macos {
        for name in [
            "SidePulse Tray.app",
            "SidePulse Settings.app",
            "SidePulse Virtual.app",
        ] {
            if !root.join("applications").join(name).is_dir() {
                return Err(fail("native package is missing an application bundle"));
            }
        }
    }
    Ok(manifest)
}
/// Build a fresh relocatable package directory without hooks or runtime state.
pub fn build(source: &Path, destination: &Path, version: &str) -> io::Result<PackageManifest> {
    let destination = crate::absolute_stage_path(destination)?;
    if fs::symlink_metadata(&destination).is_ok() {
        return Err(fail("package destination already exists"));
    }
    let plan = StagePlan::new(source, &destination, Platform::current()?)?;
    let temporary = plan.prepare()?;
    for path in ["manifest.json", "settings.json", "links.json", "relay.json"] {
        fs::remove_file(temporary.path().join(path))?;
    }
    for path in ["state", "launch"] {
        fs::remove_dir_all(temporary.path().join(path))?;
    }
    fs::write(
        temporary.path().join("README.txt"),
        "SidePulse native preview\n\nExtract this complete package into a new directory.\nRun bin/sidepulse setup --source-dir . --stage-dir PREVIEW to stage it.\nUse service install --stage-dir PREVIEW --dry-run to inspect startup.\nStop this preview before update or rollback.\nThis preview does not replace the installed Python application.\nPackage checksums detect corruption; verify platform signatures before distribution.\n",
    )?;
    let manifest = seal(temporary.path(), version)?;
    verify(temporary.path())?;
    if fs::symlink_metadata(&destination).is_ok() {
        return Err(fail("package destination appeared during build"));
    }
    fs::rename(temporary.path(), destination)?;
    Ok(manifest)
}
/// Archive final signed bytes without overwriting an existing archive.
pub fn archive(root: &Path, destination: &Path) -> io::Result<()> {
    verify(root)?;
    let parent = destination
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    let mut writer = zip::ZipWriter::new(temporary.as_file_mut());
    let mut paths = Vec::new();
    files(root, Path::new(""), &mut paths)?;
    paths.sort();
    for path in paths {
        let name = path
            .to_str()
            .ok_or_else(|| fail("package path is not Unicode"))?
            .replace('\\', "/");
        #[cfg(unix)]
        let mode = {
            use std::os::unix::fs::PermissionsExt;
            fs::metadata(root.join(&path))?.permissions().mode() & 0o777
        };
        #[cfg(not(unix))]
        let mode = if path.extension().is_some_and(|extension| extension == "exe") {
            0o755
        } else {
            0o644
        };
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Stored)
            .unix_permissions(mode);
        writer.start_file(name, options).map_err(io::Error::other)?;
        io::copy(&mut fs::File::open(root.join(path))?, &mut writer)?;
    }
    writer.finish().map_err(io::Error::other)?;
    temporary.as_file().sync_all()?;
    verify(root)?;
    temporary
        .persist_noclobber(destination)
        .map_err(|error| error.error)?;
    Ok(())
}

#[derive(Serialize)]
pub struct SigningPlan {
    pub root: PathBuf,
    pub commands: Vec<crate::startup::CommandSpec>,
    #[serde(skip)]
    manifest: PackageManifest,
}
fn signing_commands(
    platform: Platform,
    root: &Path,
    files: &[PackageFile],
    identity: &str,
    timestamp: Option<&str>,
) -> io::Result<Vec<crate::startup::CommandSpec>> {
    use crate::startup::CommandSpec;
    if identity.trim().is_empty() {
        return Err(fail("provide a signing identity"));
    }
    let mut commands = Vec::new();
    match platform {
        Platform::Macos => {
            let binaries = files.iter().filter(|file| file.path.starts_with("bin/"));
            let apps = [
                "SidePulse Tray.app",
                "SidePulse Settings.app",
                "SidePulse Virtual.app",
            ];
            for path in binaries
                .map(|file| root.join(&file.path))
                .chain(apps.map(|name| root.join("applications").join(name)))
            {
                commands.push(CommandSpec {
                    program: "/usr/bin/codesign".into(),
                    args: vec![
                        "--force".into(),
                        "--options".into(),
                        "runtime".into(),
                        "--timestamp".into(),
                        "--sign".into(),
                        identity.into(),
                        path.to_string_lossy().into(),
                    ],
                });
                commands.push(CommandSpec {
                    program: "/usr/bin/codesign".into(),
                    args: vec![
                        "--verify".into(),
                        "--deep".into(),
                        "--strict".into(),
                        path.to_string_lossy().into(),
                    ],
                });
            }
        }
        Platform::Windows => {
            if identity.len() != 40 || !identity.bytes().all(|b| b.is_ascii_hexdigit()) {
                return Err(fail(
                    "Windows signing identity must be a certificate SHA-1 thumbprint",
                ));
            }
            let timestamp = timestamp
                .filter(|url| url.starts_with("https://") && !url.chars().any(char::is_control))
                .ok_or_else(|| {
                    fail("provide --timestamp-url with an HTTPS RFC 3161 timestamp service")
                })?;
            for file in files
                .iter()
                .filter(|file| file.path.starts_with("bin/") && file.path.ends_with(".exe"))
            {
                let path = root.join(&file.path).to_string_lossy().into_owned();
                commands.push(CommandSpec {
                    program: "signtool.exe".into(),
                    args: vec![
                        "sign".into(),
                        "/sha1".into(),
                        identity.into(),
                        "/fd".into(),
                        "SHA256".into(),
                        "/tr".into(),
                        timestamp.into(),
                        "/td".into(),
                        "SHA256".into(),
                        path.clone(),
                    ],
                });
                commands.push(CommandSpec {
                    program: "signtool.exe".into(),
                    args: vec!["verify".into(), "/pa".into(), "/all".into(), path],
                });
            }
        }
        Platform::Linux => {
            return Err(fail(
                "Linux uses detached archive signing: gpg --armor --detach-sign --local-user ID RELEASE.zip",
            ));
        }
    }
    Ok(commands)
}
impl SigningPlan {
    pub fn new(root: &Path, identity: &str, timestamp: Option<&str>) -> io::Result<Self> {
        let root = fs::canonicalize(root)?;
        let manifest = verify(&root)?;
        let commands = signing_commands(
            manifest.platform,
            &root,
            &manifest.files,
            identity,
            timestamp,
        )?;
        Ok(Self {
            root,
            commands,
            manifest,
        })
    }
    pub fn apply(&self) -> io::Result<PackageManifest> {
        self.apply_with(crate::startup::run_command)
    }
    fn apply_with(
        &self,
        mut run: impl FnMut(&crate::startup::CommandSpec) -> io::Result<crate::startup::CommandResult>,
    ) -> io::Result<PackageManifest> {
        if entries(&self.root)? != self.manifest.files {
            return Err(fail("package changed after signing was planned"));
        }
        for command in &self.commands {
            let result = run(command)?;
            if !result.success {
                return Err(fail(&format!(
                    "{} failed: {}",
                    command.program,
                    result.stderr.trim()
                )));
            }
        }
        // Recompute only after platform signature verification has succeeded.
        let manifest = seal(&self.root, &self.manifest.version)?;
        verify(&self.root)?;
        Ok(manifest)
    }
}

pub const SYSTEM_PAYLOAD: &str = "/Library/Application Support/SidePulse/NativePreview";
#[derive(Serialize)]
pub struct MacInstallerPlan {
    pub root: PathBuf,
    pub destination: PathBuf,
    pub command: crate::startup::CommandSpec,
    #[serde(skip)]
    files: Vec<PackageFile>,
}
impl MacInstallerPlan {
    pub fn new(root: &Path, destination: &Path, identity: Option<&str>) -> io::Result<Self> {
        if Platform::current()? != Platform::Macos {
            return Err(fail("macOS installer creation requires macOS"));
        }
        let root = fs::canonicalize(root)?;
        let manifest = verify(&root)?;
        let destination = crate::absolute_stage_path(destination)?;
        if fs::symlink_metadata(&destination).is_ok() {
            return Err(fail("installer destination already exists"));
        }
        let version = manifest.version.split(['-', '+']).next().unwrap();
        if !version
            .split('.')
            .all(|part| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()))
        {
            return Err(fail("macOS installer requires a numeric release version"));
        }
        let mut args = vec![
            "--root".into(),
            root.to_string_lossy().into_owned(),
            "--install-location".into(),
            SYSTEM_PAYLOAD.into(),
            "--identifier".into(),
            "io.sidepulse.next.payload".into(),
            "--version".into(),
            version.into(),
            "--ownership".into(),
            "recommended".into(),
        ];
        if let Some(identity) = identity {
            if identity.trim().is_empty() {
                return Err(fail("installer identity cannot be empty"));
            }
            args.extend(["--sign".into(), identity.into(), "--timestamp".into()]);
        }
        args.push(destination.to_string_lossy().into_owned());
        Ok(Self {
            root,
            destination,
            command: crate::startup::CommandSpec {
                program: "/usr/bin/pkgbuild".into(),
                args,
            },
            files: manifest.files,
        })
    }
    pub fn apply(&self) -> io::Result<()> {
        if entries(&self.root)? != self.files {
            return Err(fail("package changed after installer planning"));
        }
        let temporary = tempfile::tempdir_in(self.destination.parent().unwrap())?;
        let output = temporary.path().join("SidePulse.pkg");
        let component_path = temporary.path().join("components.plist");
        let bundles = ["SidePulse Tray.app", "SidePulse Settings.app", "SidePulse Virtual.app"].map(|name| format!("<dict><key>RootRelativeBundlePath</key><string>applications/{name}</string><key>BundleIsRelocatable</key><false/><key>BundleIsVersionChecked</key><false/><key>BundleOverwriteAction</key><string>upgrade</string></dict>")).join("");
        fs::write(
            &component_path,
            format!(
                "<?xml version=\"1.0\"?><plist version=\"1.0\"><array>{bundles}</array></plist>"
            ),
        )?;
        let mut command = self.command.clone();
        command.args.pop();
        command.args.extend([
            "--component-plist".into(),
            component_path.to_string_lossy().into_owned(),
            output.to_string_lossy().into_owned(),
        ]);
        let result = crate::startup::run_command(&command)?;
        if !result.success {
            return Err(fail(&format!("pkgbuild failed: {}", result.stderr.trim())));
        }
        if entries(&self.root)? != self.files {
            return Err(fail("package changed while creating installer"));
        }
        let mut publication = tempfile::NamedTempFile::new_in(self.destination.parent().unwrap())?;
        io::copy(&mut fs::File::open(output)?, &mut publication)?;
        publication.as_file().sync_all()?;
        publication
            .persist_noclobber(&self.destination)
            .map_err(|error| error.error)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn relocatable_package_preserves_app_bytes_and_refuses_corruption() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("source");
        fs::create_dir(&source).unwrap();
        for name in crate::BINARIES {
            let path = source.join(format!("{name}{}", std::env::consts::EXE_SUFFIX));
            fs::write(&path, b"native binary").unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
            }
        }
        let package = directory.path().join("package");
        build(&source, &package, "0.1.0-preview").unwrap();
        assert!(!package.join("settings.json").exists());
        assert!(!package.join("state").exists());
        assert!(!package.join("manifest.json").exists());
        // A signing tool can change bundle executables and add signature resources.
        if Platform::current().unwrap() == Platform::Macos {
            let app = package.join("applications/SidePulse Tray.app/Contents");
            fs::write(app.join("MacOS/sidepulse-next-tray"), b"signed app binary").unwrap();
            fs::create_dir(app.join("_CodeSignature")).unwrap();
            fs::write(
                app.join("_CodeSignature/CodeResources"),
                b"signature envelope",
            )
            .unwrap();
            assert!(verify(&package).is_err());
            seal(&package, "0.1.0-preview").unwrap();
        }
        let preview = directory.path().join("relocated preview");
        StagePlan::new(&package, &preview, Platform::current().unwrap())
            .unwrap()
            .stage()
            .unwrap();
        if Platform::current().unwrap() == Platform::Macos {
            assert_eq!(
                tree_hash(&package.join("applications")).unwrap(),
                tree_hash(&preview.join("applications")).unwrap()
            );
            let backup = directory.path().join("backup");
            crate::upgrade::UpdatePlan::new(&package, &preview, &backup)
                .unwrap()
                .apply()
                .unwrap();
            assert_eq!(
                tree_hash(&package.join("applications")).unwrap(),
                tree_hash(&preview.join("applications")).unwrap()
            );
        }
        let archive_path = directory.path().join("release.zip");
        archive(&package, &archive_path).unwrap();
        assert!(archive(&package, &archive_path).is_err());
        let mut zip = zip::ZipArchive::new(fs::File::open(archive_path).unwrap()).unwrap();
        let name = format!("bin/sidepulse-next{}", std::env::consts::EXE_SUFFIX);
        assert_eq!(
            zip.by_name(&name).unwrap().unix_mode().unwrap() & 0o777,
            0o755
        );
        fs::write(package.join(&name), b"corrupt").unwrap();
        assert!(verify(&package).is_err());
        assert!(
            StagePlan::new(
                &package,
                &directory.path().join("refused"),
                Platform::current().unwrap()
            )
            .is_err()
        );
        assert!(build(&source, &package, "0.1.0").is_err());
    }
    #[test]
    fn signing_adapters_verify_platform_signatures_with_literal_arguments() {
        let files = vec![PackageFile {
            path: "bin/example.exe".into(),
            bytes: 1,
            sha256: String::new(),
            executable: true,
        }];
        let root = Path::new("/literal path & quoted");
        let mac =
            signing_commands(Platform::Macos, root, &files, "Developer ID: example", None).unwrap();
        assert!(mac[0].args.contains(&"runtime".into()));
        assert_eq!(
            mac[0].args.last().unwrap(),
            &root.join("bin/example.exe").to_string_lossy()
        );
        assert_eq!(mac.last().unwrap().args[0], "--verify");
        let identity = "0123456789abcdef0123456789abcdef01234567";
        let windows = signing_commands(
            Platform::Windows,
            root,
            &files,
            identity,
            Some("https://timestamp.example.test"),
        )
        .unwrap();
        assert!(windows[0].args.contains(&"SHA256".into()));
        assert!(windows[1].args.contains(&"/pa".into()));
        assert!(signing_commands(Platform::Windows, root, &files, identity, None).is_err());
        assert!(
            signing_commands(
                Platform::Windows,
                root,
                &files,
                "not-a-thumbprint",
                Some("https://timestamp.example.test")
            )
            .is_err()
        );
    }

    #[cfg(unix)]
    #[test]
    fn package_rejects_symlinks_and_preserves_signed_resource_integrity() {
        let directory = tempfile::tempdir().unwrap();
        fs::write(directory.path().join("ordinary"), b"data").unwrap();
        std::os::unix::fs::symlink(
            directory.path().join("ordinary"),
            directory.path().join("linked"),
        )
        .unwrap();
        assert!(seal(directory.path(), "0.1.0").is_err());
    }
}
