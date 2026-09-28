//! Explicit copying into an unpublished preview. No production ownership changes.
use crate::upgrade::{bounded_read, files};
use serde::Serialize;
use std::{
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
};
#[derive(Serialize)]
pub struct LegacyImport {
    pub files: Vec<PathBuf>,
    #[serde(skip)]
    data: Vec<ImportedFile>,
}
struct ImportedFile {
    source: PathBuf,
    target: PathBuf,
    bytes: Vec<u8>,
}
impl LegacyImport {
    pub fn new(config: Option<&Path>, logs: Option<&Path>) -> io::Result<Self> {
        let mut data = Vec::new();
        if let Some(config) = config {
            let config = fs::canonicalize(config)?;
            for name in ["settings.json", "links.json"] {
                let source = config.join(name);
                match fs::symlink_metadata(&source) {
                    Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                    Err(error) => return Err(error),
                    Ok(_) => {}
                }
                let bytes = bounded_read(&source, 4 * 1024 * 1024)?;
                if !serde_json::from_slice::<serde_json::Value>(&bytes)?.is_object() {
                    return Err(io::Error::other(
                        "legacy configuration must be a JSON object",
                    ));
                }
                data.push(ImportedFile {
                    source,
                    target: name.into(),
                    bytes,
                });
            }
            let mut paths = Vec::new();
            files(&config, Path::new("animations"), &mut paths)?;
            for path in paths {
                if path.components().count() != 2
                    || !path
                        .extension()
                        .is_some_and(|extension| extension.eq_ignore_ascii_case("LED"))
                {
                    return Err(io::Error::other(
                        "legacy animation directory must contain ordinary LED files",
                    ));
                }
                let source = config.join(&path);
                let bytes = bounded_read(&source, 65536)?;
                data.push(ImportedFile {
                    source,
                    target: path,
                    bytes,
                });
            }
        }
        if let Some(logs) = logs {
            let logs = fs::canonicalize(logs)?;
            for name in [
                "codex.jsonl",
                "claude.jsonl",
                "grok.jsonl",
                "cursor.jsonl",
                "junie.jsonl",
                "event-status.jsonl",
                "status-history.jsonl",
                "latest.json",
            ] {
                let source = logs.join(name);
                match fs::symlink_metadata(&source) {
                    Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                    Err(error) => return Err(error),
                    Ok(_) => {}
                }
                let bytes = bounded_read(&source, 16 * 1024 * 1024)?;
                data.push(ImportedFile {
                    source,
                    target: PathBuf::from("state").join(name),
                    bytes,
                });
            }
        }
        if data.iter().map(|file| file.bytes.len()).sum::<usize>() > 128 * 1024 * 1024 {
            return Err(io::Error::other("legacy import exceeds 128 MiB"));
        }
        Ok(Self {
            files: data.iter().map(|file| file.target.clone()).collect(),
            data,
        })
    }
    pub(crate) fn check(&self) -> io::Result<()> {
        for file in &self.data {
            if bounded_read(&file.source, 16 * 1024 * 1024)? != file.bytes {
                return Err(io::Error::other(
                    "legacy source changed after import planning",
                ));
            }
        }
        Ok(())
    }
    pub(crate) fn copy(&self, target: &Path) -> io::Result<()> {
        self.check()?;
        for file in &self.data {
            let destination = target.join(&file.target);
            fs::create_dir_all(destination.parent().unwrap())?;
            let mut temporary = tempfile::NamedTempFile::new_in(destination.parent().unwrap())?;
            temporary.write_all(&file.bytes)?;
            temporary.as_file().sync_all()?;
            temporary
                .persist(destination)
                .map_err(|error| error.error)?;
        }
        self.check()
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn raw_settings_links_named_assets_and_logs_copy_without_activating_relay() {
        let directory = tempfile::tempdir().unwrap();
        let config = directory.path().join("config");
        let logs = directory.path().join("logs");
        let target = directory.path().join("preview");
        fs::create_dir_all(config.join("animations")).unwrap();
        fs::create_dir(&logs).unwrap();
        fs::create_dir(&target).unwrap();
        fs::write(config.join("settings.json"),b"{\"unknown\":true,\"custom_agent_animations\":{\"custom:sample\":{\"file\":\"sample.LED\"}}}\n").unwrap();
        fs::write(
            config.join("links.json"),
            b"{\"version\":1,\"ios\":[],\"unknown\":7}\n",
        )
        .unwrap();
        fs::write(config.join("relay.json"), b"{\"enabled\":true}").unwrap();
        fs::write(config.join("animations/sample.LED"), b"#00FF80").unwrap();
        fs::write(logs.join("junie.jsonl"), b"captured audit\n").unwrap();
        for name in ["event-status.jsonl", "status-history.jsonl", "latest.json"] {
            fs::write(logs.join(name), b"{\"retained\":true}\n").unwrap();
        }
        let import = LegacyImport::new(Some(&config), Some(&logs)).unwrap();
        import.copy(&target).unwrap();
        assert_eq!(
            fs::read(config.join("settings.json")).unwrap(),
            fs::read(target.join("settings.json")).unwrap()
        );
        assert_eq!(
            fs::read(config.join("links.json")).unwrap(),
            fs::read(target.join("links.json")).unwrap()
        );
        assert_eq!(
            fs::read(target.join("animations/sample.LED")).unwrap(),
            b"#00FF80"
        );
        assert!(target.join("state/junie.jsonl").is_file());
        for name in ["event-status.jsonl", "status-history.jsonl", "latest.json"] {
            assert_eq!(
                fs::read(logs.join(name)).unwrap(),
                fs::read(target.join("state").join(name)).unwrap()
            );
        }
        assert!(!target.join("relay.json").exists());
        fs::write(config.join("settings.json"), b"{}").unwrap();
        assert!(import.check().is_err());
    }
}
