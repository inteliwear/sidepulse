//! Reversible updates of an offline native preview, retaining its runtime paths.
use crate::{Platform, StagePlan, read_stage};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};
const RECEIPT: &str = ".sidepulse-backup.json";
const LIMIT: u64 = 128 * 1024 * 1024;
#[derive(Clone, Debug, PartialEq, Eq)]
struct DataFile {
    path: PathBuf,
    bytes: Vec<u8>,
}
#[derive(Serialize, Deserialize, PartialEq, Eq)]
struct Receipt {
    version: u8,
    target: PathBuf,
    manifest_sha256: String,
    payload_sha256: String,
}
#[derive(Serialize)]
pub struct UpdatePlan {
    pub destination: PathBuf,
    pub backup: PathBuf,
    pub source_dir: PathBuf,
    pub endpoint: String,
    pub preserved_files: Vec<PathBuf>,
    #[serde(skip)]
    stage: StagePlan,
    #[serde(skip)]
    manifest: Vec<u8>,
    #[serde(skip)]
    data: Vec<DataFile>,
    #[serde(skip)]
    source_hash: String,
    #[serde(skip)]
    payload: String,
}
#[derive(Serialize)]
pub struct RollbackPlan {
    pub destination: PathBuf,
    pub backup: PathBuf,
    pub replaced_bundle: PathBuf,
    pub endpoint: String,
    pub preserved_files: Vec<PathBuf>,
    #[serde(skip)]
    manifest: Vec<u8>,
    #[serde(skip)]
    data: Vec<DataFile>,
    #[serde(skip)]
    receipt: Receipt,
    #[serde(skip)]
    payload: String,
}
fn fail(message: &str) -> io::Error {
    io::Error::other(message)
}
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn manifest_bytes(root: &Path) -> io::Result<Vec<u8>> {
    bounded_read(&root.join("manifest.json"), 1024 * 1024)
}
pub(crate) fn bounded_read(path: &Path, limit: u64) -> io::Result<Vec<u8>> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_file() || meta.len() > limit {
        return Err(fail("bundle data is not a bounded regular file"));
    }
    let mut bytes = Vec::new();
    fs::File::open(path)?
        .take(limit + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit {
        return Err(fail("bundle data grew beyond its limit"));
    }
    Ok(bytes)
}
pub(crate) fn files(root: &Path, relative: &Path, result: &mut Vec<PathBuf>) -> io::Result<()> {
    let path = root.join(relative);
    let meta = match fs::symlink_metadata(&path) {
        Ok(meta) => meta,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    if meta.file_type().is_symlink() {
        return Err(fail("bundle contains a symlink"));
    }
    if relative.components().count() > 64 {
        return Err(fail("bundle directory nesting exceeds limit"));
    }
    if meta.is_dir() {
        for entry in fs::read_dir(path)? {
            files(root, &relative.join(entry?.file_name()), result)?;
        }
    } else if meta.is_file() {
        result.push(relative.to_path_buf());
    } else {
        #[cfg(unix)]
        {
            use std::os::unix::fs::FileTypeExt;
            if relative == Path::new("state/events.sock") && meta.file_type().is_socket() {
                return Ok(());
            }
        }
        return Err(fail("bundle contains a symlink or special file"));
    }
    if result.len() > 10000 {
        return Err(fail("bundle contains too many files"));
    }
    Ok(())
}
fn snapshot(root: &Path) -> io::Result<Vec<DataFile>> {
    let mut paths = Vec::new();
    for path in [
        "settings.json",
        "links.json",
        "relay.json",
        "state",
        "animations",
    ] {
        files(root, Path::new(path), &mut paths)?;
    }
    paths.sort();
    let mut total = 0;
    let mut result = Vec::new();
    for path in paths {
        let bytes = bounded_read(&root.join(&path), LIMIT)?;
        total += bytes.len() as u64;
        if total > LIMIT {
            return Err(fail("preview state exceeds the 128 MiB migration limit"));
        }
        result.push(DataFile { path, bytes });
    }
    Ok(result)
}
fn write_private(path: &Path, bytes: &[u8]) -> io::Result<()> {
    fs::create_dir_all(path.parent().ok_or_else(|| fail("file needs a parent"))?)?;
    let mut temporary = tempfile::NamedTempFile::new_in(path.parent().unwrap())?;
    temporary.write_all(bytes)?;
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|e| e.error)?;
    #[cfg(unix)]
    fs::File::open(path.parent().unwrap())?.sync_all()?;
    Ok(())
}
fn copy_data(root: &Path, data: &[DataFile]) -> io::Result<()> {
    // Only used on unpublished temporary trees. Remove deleted runtime files too.
    for relative in [
        "settings.json",
        "links.json",
        "relay.json",
        "state",
        "animations",
    ] {
        let path = root.join(relative);
        match fs::symlink_metadata(&path) {
            Ok(meta) if meta.is_dir() => fs::remove_dir_all(path)?,
            Ok(_) => fs::remove_file(path)?,
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(e),
        }
    }
    fs::create_dir_all(root.join("state"))?;
    for file in data {
        write_private(&root.join(&file.path), &file.bytes)?;
    }
    Ok(())
}
fn payload_hash(root: &Path) -> io::Result<String> {
    let mut paths = Vec::new();
    for path in ["bin", "applications", "launch", "manifest.json"] {
        files(root, Path::new(path), &mut paths)?;
    }
    paths.sort();
    hash_paths(root, &paths)
}
pub(crate) fn hash_paths(root: &Path, paths: &[PathBuf]) -> io::Result<String> {
    let mut digest = Sha256::new();
    let mut total = 0;
    let mut buffer = [0u8; 65536];
    for path in paths {
        let meta = fs::symlink_metadata(root.join(path))?;
        if !meta.is_file() {
            return Err(fail("payload file is not regular"));
        }
        total += meta.len();
        if total > 512 * 1024 * 1024 {
            return Err(fail("native payload exceeds 512 MiB"));
        }
        let relative = path.to_string_lossy();
        digest.update((relative.len() as u64).to_le_bytes());
        digest.update(relative.as_bytes());
        digest.update(meta.len().to_le_bytes());
        let mut file = fs::File::open(root.join(path))?.take(meta.len() + 1);
        let mut read = 0;
        loop {
            let count = file.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            read += count as u64;
            digest.update(&buffer[..count]);
        }
        if read != meta.len() {
            return Err(fail("payload changed while being read"));
        }
    }
    Ok(format!("{:x}", digest.finalize()))
}
fn source_hash(root: &Path) -> io::Result<String> {
    crate::package::verify_source(root)?;
    let mut digest = Sha256::new();
    digest.update(binaries_hash(root)?);
    for name in [
        "SidePulse Tray.app",
        "SidePulse Settings.app",
        "SidePulse Virtual.app",
    ] {
        if let Some(app) = crate::package::application_source(root, std::ffi::OsStr::new(name)) {
            digest.update(name);
            digest.update(crate::package::tree_hash(&app)?);
        }
    }
    Ok(format!("{:x}", digest.finalize()))
}
fn binaries_hash(root: &Path) -> io::Result<String> {
    hash_paths(
        root,
        &crate::BINARIES
            .iter()
            .map(|name| PathBuf::from(format!("{name}{}", std::env::consts::EXE_SUFFIX)))
            .collect::<Vec<_>>(),
    )
}
fn vacant_sibling(destination: &Path, backup: &Path) -> io::Result<PathBuf> {
    let backup = crate::absolute_stage_path(backup)?;
    if backup.parent() != destination.parent() || backup == destination {
        return Err(fail(
            "backup must be a different sibling directory on the same filesystem",
        ));
    }
    absent(&backup)?;
    Ok(backup)
}
fn absent(path: &Path) -> io::Result<()> {
    match fs::symlink_metadata(path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
        Ok(_) => Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "destination already exists",
        )),
    }
}
fn restore_after_failure(saved: &Path, destination: &Path, error: io::Error) -> io::Error {
    if let Err(restore) = absent(destination).and_then(|()| fs::rename(saved, destination)) {
        return fail(&format!(
            "publication failed: {error}; original bundle is saved at {} and could not be restored: {restore}",
            saved.display()
        ));
    }
    error
}
fn verified_receipt(backup: &Path, destination: &Path) -> io::Result<Receipt> {
    crate::read_stage_at(backup, destination)?;
    let receipt: Receipt =
        serde_json::from_slice(&bounded_read(&backup.join(RECEIPT), 1024 * 1024)?)?;
    if receipt.version != 1
        || receipt.target != destination
        || receipt.manifest_sha256 != hash(&manifest_bytes(backup)?)
        || receipt.payload_sha256 != payload_hash(backup)?
    {
        return Err(fail(
            "backup identity or immutable payload checksum differs",
        ));
    }
    Ok(receipt)
}
fn offline(endpoint: &str) -> io::Result<()> {
    use sidepulse_core::{ClientRequest, PROTOCOL_VERSION, RequestKind, ServerMessage};
    match sidepulse_ipc::request::<_, ServerMessage>(
        endpoint,
        &ClientRequest {
            version: PROTOCOL_VERSION,
            request_id: 1,
            kind: RequestKind::Snapshot,
        },
        Duration::from_millis(300),
    ) {
        Ok(_) => Err(fail(
            "stop this preview's service before replacing its bundle",
        )),
        Err(e)
            if matches!(
                e.kind(),
                io::ErrorKind::NotFound
                    | io::ErrorKind::ConnectionRefused
                    | io::ErrorKind::NotConnected
            ) =>
        {
            Ok(())
        }
        Err(e) => Err(e),
    }
}
fn update_lock(destination: &Path) -> io::Result<fs::File> {
    let name = destination
        .file_name()
        .ok_or_else(|| fail("bundle needs a name"))?
        .to_string_lossy();
    let path = destination
        .parent()
        .ok_or_else(|| fail("bundle needs a parent"))?
        .join(format!(".{name}.sidepulse-update.lock"));
    if fs::symlink_metadata(&path).is_ok_and(|meta| !meta.is_file()) {
        return Err(fail("update lock is not a regular file"));
    }
    let mut options = fs::OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let file = options.open(path)?;
    file.try_lock()
        .map_err(|error| fail(&format!("cannot acquire native update lock: {error}")))?;
    Ok(file)
}
fn verify_prepared_binaries(root: &Path, stage: &StagePlan, expected: &str) -> io::Result<()> {
    if source_hash(&stage.source_dir)? != expected
        || binaries_hash(&root.join("bin"))? != binaries_hash(&stage.source_dir)?
    {
        return Err(fail("prepared binaries differ from the planned payload"));
    }
    for (bundle, index) in stage.manifest.application_bundles.iter().zip([3, 5, 6]) {
        let name = stage.manifest.binaries[index].file_name().unwrap();
        let app = root.join("applications").join(bundle.file_name().unwrap());
        let matches = if let Some(source) =
            crate::package::application_source(&stage.source_dir, bundle.file_name().unwrap())
        {
            crate::package::tree_hash(&source)? == crate::package::tree_hash(&app)?
        } else {
            hash_paths(&root.join("bin"), &[name.into()])?
                == hash_paths(&app.join("Contents/MacOS"), &[name.into()])?
        };
        if !matches {
            return Err(fail(
                "prepared application binary differs from the planned payload",
            ));
        }
    }
    Ok(())
}
impl UpdatePlan {
    pub fn new(source: &Path, destination: &Path, backup: &Path) -> io::Result<Self> {
        let manifest = read_stage(destination)?;
        let destination = manifest.stage_dir.clone();
        let backup = vacant_sibling(&destination, backup)?;
        let stage = StagePlan::new_internal(source, &destination, Platform::current()?, true)?;
        let data = snapshot(&destination)?;
        let source_dir = crate::package::resolve_source(source)?;
        Ok(Self {
            destination: destination.clone(),
            backup,
            source_hash: source_hash(&source_dir)?,
            payload: payload_hash(&destination)?,
            source_dir,
            endpoint: manifest.endpoint,
            preserved_files: data.iter().map(|f| f.path.clone()).collect(),
            stage,
            manifest: manifest_bytes(&destination)?,
            data,
        })
    }
    pub fn apply(&self) -> io::Result<()> {
        let _lock = update_lock(&self.destination)?;
        offline(&self.endpoint)?;
        if payload_hash(&self.destination)? != self.payload
            || manifest_bytes(&self.destination)? != self.manifest
            || snapshot(&self.destination)? != self.data
            || source_hash(&self.source_dir)? != self.source_hash
        {
            return Err(fail(
                "bundle, state, or incoming binaries changed after planning",
            ));
        }
        vacant_sibling(&self.destination, &self.backup)?;
        let temporary = self.stage.prepare()?;
        verify_prepared_binaries(temporary.path(), &self.stage, &self.source_hash)?;
        copy_data(temporary.path(), &self.data)?;
        let receipt = Receipt {
            version: 1,
            target: self.destination.clone(),
            manifest_sha256: hash(&self.manifest),
            payload_sha256: self.payload.clone(),
        };
        if payload_hash(&self.destination)? != self.payload
            || snapshot(&self.destination)? != self.data
            || source_hash(&self.source_dir)? != self.source_hash
        {
            return Err(fail(
                "bundle state or incoming binaries changed while preparing update",
            ));
        }
        // The receipt is durable before the original moves, so a crash in the
        // publication gap can always be recovered without guessing ownership.
        write_private(
            &self.destination.join(RECEIPT),
            &serde_json::to_vec(&receipt)?,
        )?;
        vacant_sibling(&self.destination, &self.backup)?;
        fs::rename(&self.destination, &self.backup)?;
        let result = absent(&self.destination)
            .and_then(|()| fs::rename(temporary.path(), &self.destination));
        if let Err(error) = result {
            return Err(restore_after_failure(
                &self.backup,
                &self.destination,
                error,
            ));
        }
        Ok(())
    }
}
impl RollbackPlan {
    pub fn new(destination: &Path, backup: &Path, replaced_bundle: &Path) -> io::Result<Self> {
        let current = read_stage(destination)?;
        let destination = current.stage_dir.clone();
        let backup = fs::canonicalize(backup)?;
        if backup.parent() != destination.parent() || backup == destination {
            return Err(fail("backup must be a sibling of the selected preview"));
        }
        let receipt = verified_receipt(&backup, &destination)?;
        let replaced_bundle = vacant_sibling(&destination, replaced_bundle)?;
        let data = snapshot(&destination)?;
        Ok(Self {
            destination: destination.clone(),
            backup,
            replaced_bundle,
            endpoint: current.endpoint,
            preserved_files: data.iter().map(|f| f.path.clone()).collect(),
            manifest: manifest_bytes(&destination)?,
            data,
            receipt,
            payload: payload_hash(&destination)?,
        })
    }
    pub fn apply(&self) -> io::Result<()> {
        let _lock = update_lock(&self.destination)?;
        offline(&self.endpoint)?;
        if payload_hash(&self.destination)? != self.payload
            || manifest_bytes(&self.destination)? != self.manifest
            || snapshot(&self.destination)? != self.data
            || payload_hash(&self.backup)? != self.receipt.payload_sha256
            || serde_json::from_slice::<Receipt>(&bounded_read(
                &self.backup.join(RECEIPT),
                1024 * 1024,
            )?)? != self.receipt
        {
            return Err(fail("current state or backup changed after planning"));
        }
        vacant_sibling(&self.destination, &self.replaced_bundle)?;
        let temporary = tempfile::Builder::new()
            .prefix(".sidepulse-rollback-")
            .tempdir_in(self.destination.parent().unwrap())?;
        let mut paths = Vec::new();
        files(&self.backup, Path::new(""), &mut paths)?;
        let mut total = 0;
        for relative in paths {
            if relative == Path::new(RECEIPT) {
                continue;
            }
            total += fs::metadata(self.backup.join(&relative))?.len();
            if total > 1024 * 1024 * 1024 {
                return Err(fail("rollback bundle exceeds 1 GiB"));
            }
            let target = temporary.path().join(&relative);
            fs::create_dir_all(target.parent().unwrap())?;
            fs::copy(self.backup.join(relative), target)?;
        }
        copy_data(temporary.path(), &self.data)?;
        if payload_hash(&self.destination)? != self.payload
            || snapshot(&self.destination)? != self.data
            || payload_hash(&self.backup)? != self.receipt.payload_sha256
        {
            return Err(fail("state or backup changed while preparing rollback"));
        }
        let receipt = Receipt {
            version: 1,
            target: self.destination.clone(),
            manifest_sha256: hash(&self.manifest),
            payload_sha256: self.payload.clone(),
        };
        write_private(
            &self.destination.join(RECEIPT),
            &serde_json::to_vec(&receipt)?,
        )?;
        vacant_sibling(&self.destination, &self.replaced_bundle)?;
        fs::rename(&self.destination, &self.replaced_bundle)?;
        if let Err(error) =
            absent(&self.destination).and_then(|()| fs::rename(temporary.path(), &self.destination))
        {
            return Err(restore_after_failure(
                &self.replaced_bundle,
                &self.destination,
                error,
            ));
        }
        Ok(())
    }
}

/// Restore an original bundle after interruption in the directory publication gap.
/// Recovery is allowed only while its original location is absent.
#[derive(Serialize)]
pub struct RecoveryPlan {
    pub destination: PathBuf,
    pub backup: PathBuf,
    pub endpoint: String,
    #[serde(skip)]
    receipt: Receipt,
}
impl RecoveryPlan {
    pub fn new(destination: &Path, backup: &Path) -> io::Result<Self> {
        let destination = crate::absolute_stage_path(destination)?;
        absent(&destination)?;
        let backup = fs::canonicalize(backup)?;
        if backup.parent() != destination.parent() || backup == destination {
            return Err(fail("backup must be a sibling of the selected preview"));
        }
        let receipt = verified_receipt(&backup, &destination)?;
        let manifest = crate::read_stage_at(&backup, &destination)?;
        Ok(Self {
            destination,
            backup,
            endpoint: manifest.endpoint,
            receipt,
        })
    }
    pub fn apply(&self) -> io::Result<()> {
        let _lock = update_lock(&self.destination)?;
        offline(&self.endpoint)?;
        absent(&self.destination)?;
        if verified_receipt(&self.backup, &self.destination)? != self.receipt {
            return Err(fail("backup changed after recovery planning"));
        }
        fs::rename(&self.backup, &self.destination)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Fixture {
        _directory: tempfile::TempDir,
        root: PathBuf,
        incoming: PathBuf,
        backup: PathBuf,
        saved: PathBuf,
    }
    impl Fixture {
        fn new() -> Self {
            let directory = tempfile::tempdir().unwrap();
            let source = directory.path().join("old");
            let incoming = directory.path().join("new");
            let root = directory.path().join("preview");
            let backup = directory.path().join("backup");
            let saved = directory.path().join("saved");
            for (path, bytes) in [(&source, b"old"), (&incoming, b"new")] {
                fs::create_dir(path).unwrap();
                for name in crate::BINARIES {
                    fs::write(
                        path.join(format!("{name}{}", std::env::consts::EXE_SUFFIX)),
                        bytes,
                    )
                    .unwrap();
                }
            }
            StagePlan::new(&source, &root, Platform::current().unwrap())
                .unwrap()
                .stage()
                .unwrap();
            fs::write(
                root.join("settings.json"),
                b"{\"unknown\":7,\"brightness\":0.7}\n",
            )
            .unwrap();
            fs::write(root.join("links.json"), b"{\"unknown\":\"keep\"}\n").unwrap();
            fs::write(root.join("relay.json"), b"{\"token\":\"private\"}\n").unwrap();
            fs::create_dir_all(root.join("state/custom")).unwrap();
            fs::write(
                root.join("state/custom/activity.log"),
                b"unknown log fields\n",
            )
            .unwrap();
            fs::create_dir_all(root.join("animations")).unwrap();
            fs::write(root.join("animations/custom.LED"), b"#00FF80").unwrap();
            Self {
                _directory: directory,
                root,
                incoming,
                backup,
                saved,
            }
        }
        fn update(&self) -> UpdatePlan {
            UpdatePlan::new(&self.incoming, &self.root, &self.backup).unwrap()
        }
        fn cli(&self) -> PathBuf {
            self.root.join(format!(
                "bin/sidepulse-next{}",
                std::env::consts::EXE_SUFFIX
            ))
        }
    }
    #[test]
    fn update_and_rollback_preserve_latest_raw_state_and_stable_paths() {
        let f = Fixture::new();
        let manifest = read_stage(&f.root).unwrap();
        let before = snapshot(&f.root).unwrap();
        f.update().apply().unwrap();
        assert_eq!(fs::read(f.cli()).unwrap(), b"new");
        assert_eq!(snapshot(&f.root).unwrap(), before);
        assert_eq!(read_stage(&f.root).unwrap(), manifest);
        assert_eq!(
            fs::read(f.backup.join("settings.json")).unwrap(),
            before
                .iter()
                .find(|d| d.path == Path::new("settings.json"))
                .unwrap()
                .bytes
        );
        fs::write(f.root.join("settings.json"), b"{\"unknown\":\"latest\"}\n").unwrap();
        fs::remove_file(f.root.join("links.json")).unwrap();
        fs::write(f.root.join("state/new.log"), b"latest history\n").unwrap();
        fs::write(f.root.join("animations/custom.LED"), b"#FF0080").unwrap();
        let latest = snapshot(&f.root).unwrap();
        RollbackPlan::new(&f.root, &f.backup, &f.saved)
            .unwrap()
            .apply()
            .unwrap();
        assert_eq!(fs::read(f.cli()).unwrap(), b"old");
        assert_eq!(snapshot(&f.root).unwrap(), latest);
        assert!(!f.root.join("links.json").exists());
        assert_eq!(read_stage(&f.root).unwrap(), manifest);
        RollbackPlan::new(&f.root, &f.saved, &f.root.with_file_name("redo-saved"))
            .unwrap()
            .apply()
            .unwrap();
        assert_eq!(fs::read(f.cli()).unwrap(), b"new");
        assert_eq!(snapshot(&f.root).unwrap(), latest);
    }
    #[test]
    fn changed_state_binary_or_destination_refuses_without_replacing_original() {
        for change in 0..4 {
            let f = Fixture::new();
            let plan = f.update();
            match change {
                0 => fs::write(f.root.join("settings.json"), b"external edit").unwrap(),
                1 => fs::write(
                    f.incoming
                        .join(format!("sidepulse-next{}", std::env::consts::EXE_SUFFIX)),
                    b"edited incoming",
                )
                .unwrap(),
                2 => fs::write(f.cli(), b"edited current").unwrap(),
                _ => fs::create_dir(&f.backup).unwrap(),
            }
            let original = fs::read(f.cli()).unwrap();
            let state = snapshot(&f.root).unwrap();
            assert!(plan.apply().is_err());
            assert_eq!(fs::read(f.cli()).unwrap(), original);
            assert_eq!(snapshot(&f.root).unwrap(), state);
        }
    }
    #[test]
    fn altered_backup_refuses_rollback_and_recovery() {
        let f = Fixture::new();
        f.update().apply().unwrap();
        let plan = RollbackPlan::new(&f.root, &f.backup, &f.saved).unwrap();
        fs::write(
            f.backup.join(format!(
                "bin/sidepulse-next{}",
                std::env::consts::EXE_SUFFIX
            )),
            b"tampered",
        )
        .unwrap();
        assert!(plan.apply().is_err());
        assert!(!f.saved.exists());
        assert_eq!(fs::read(f.cli()).unwrap(), b"new");
        assert!(RecoveryPlan::new(&f.root.with_file_name("absent"), &f.backup).is_err());
    }
    #[test]
    fn lock_refuses_concurrent_update() {
        let f = Fixture::new();
        let plan = f.update();
        let _lock = update_lock(&f.root).unwrap();
        assert!(plan.apply().unwrap_err().to_string().contains("lock"));
        assert_eq!(fs::read(f.cli()).unwrap(), b"old");
        assert!(!f.backup.exists());
    }
    #[test]
    fn interrupted_publication_recovers_original_and_never_overwrites_new_directory() {
        let f = Fixture::new();
        f.update().apply().unwrap();
        // Model a crash after the original directory moved and before publication.
        fs::rename(&f.root, &f.saved).unwrap();
        let plan = RecoveryPlan::new(&f.root, &f.backup).unwrap();
        fs::create_dir(&f.root).unwrap();
        fs::write(f.root.join("external"), b"retain").unwrap();
        assert!(plan.apply().is_err());
        assert_eq!(fs::read(f.root.join("external")).unwrap(), b"retain");
        fs::remove_file(f.root.join("external")).unwrap();
        fs::remove_dir(&f.root).unwrap();
        plan.apply().unwrap();
        assert_eq!(fs::read(f.cli()).unwrap(), b"old");
        assert!(!f.backup.exists());
        assert!(read_stage(&f.root).is_ok());
    }
    #[test]
    fn failed_publication_restores_original_or_preserves_unexpected_replacement() {
        let f = Fixture::new();
        fs::rename(&f.root, &f.backup).unwrap();
        let error = restore_after_failure(&f.backup, &f.root, fail("injected rename failure"));
        assert!(error.to_string().contains("injected"));
        assert_eq!(fs::read(f.cli()).unwrap(), b"old");
        fs::rename(&f.root, &f.backup).unwrap();
        fs::create_dir(&f.root).unwrap();
        let error = restore_after_failure(&f.backup, &f.root, fail("injected rename failure"));
        assert!(error.to_string().contains("could not be restored"));
        assert!(f.backup.is_dir());
        assert!(f.root.is_dir());
    }
    #[cfg(unix)]
    #[test]
    fn active_endpoint_and_state_symlinks_are_refused() {
        let f = Fixture::new();
        let plan = f.update();
        let listener = std::os::unix::net::UnixListener::bind(&plan.endpoint).unwrap();
        let peer = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut data = [0u8; 1024];
            let _ = stream.read(&mut data);
            stream.write_all(b"{}\n").unwrap();
        });
        assert!(plan.apply().is_err());
        peer.join().unwrap();
        assert!(!f.backup.exists());
        fs::remove_file(&plan.endpoint).unwrap();
        std::os::unix::fs::symlink(f.root.join("settings.json"), f.root.join("state/symlink"))
            .unwrap();
        assert!(UpdatePlan::new(&f.incoming, &f.root, &f.backup).is_err());
    }
}
