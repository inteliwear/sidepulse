//! Atomic relay settings updates with unknown-field retention and conflict checks.

use crate::RelayConfig;
use serde_json::Value;
use std::{
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
};
use tempfile::NamedTempFile;

pub struct RelayConfigStore {
    path: PathBuf,
    original: Option<Vec<u8>>,
    document: serde_json::Map<String, Value>,
    config: RelayConfig,
}
impl RelayConfigStore {
    pub fn load(path: &Path, host: &str) -> io::Result<Self> {
        let original = match fs::read(path) {
            Ok(bytes) if bytes.len() <= 1024 * 1024 => Some(bytes),
            Ok(_) => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "relay settings are too large",
                ));
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        let document = if let Some(bytes) = &original {
            serde_json::from_slice::<Value>(bytes)?
                .as_object()
                .cloned()
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "relay settings must be an object",
                    )
                })?
        } else {
            serde_json::Map::new()
        };
        let config = RelayConfig::from_legacy_json(&Value::Object(document.clone()), host);
        Ok(Self {
            path: path.into(),
            original,
            document,
            config,
        })
    }
    pub fn config(&self) -> &RelayConfig {
        &self.config
    }
    pub fn path(&self) -> &Path {
        &self.path
    }
    pub fn save(&mut self, config: RelayConfig) -> io::Result<()> {
        let mut document = self.document.clone();
        document.extend(config.to_legacy_json()?.as_object().unwrap().clone());
        if fs::symlink_metadata(&self.path).is_ok_and(|metadata| metadata.file_type().is_symlink())
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "refusing to replace a relay config symlink",
            ));
        }
        let current = match fs::read(&self.path) {
            Ok(bytes) => Some(bytes),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        if current != self.original {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "relay settings changed externally; reload before saving",
            ));
        }
        let parent = self.path.parent().unwrap_or(Path::new("."));
        fs::create_dir_all(parent)?;
        let mut temporary = NamedTempFile::new_in(parent)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            temporary
                .as_file()
                .set_permissions(fs::Permissions::from_mode(0o600))?;
        }
        let mut bytes = serde_json::to_vec_pretty(&document)?;
        bytes.push(b'\n');
        temporary.write_all(&bytes)?;
        temporary.as_file().sync_all()?;
        temporary.persist(&self.path).map_err(|error| error.error)?;
        self.original = Some(bytes);
        self.document = document;
        self.config = config;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn saves_preserve_unknown_fields_and_reject_external_edits() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("relay.json");
        fs::write(&path, r#"{"version":1,"unknown":{"keep":7}}"#).unwrap();
        let mut store = RelayConfigStore::load(&path, "Test").unwrap();
        let config = store.config().clone().with_receiver_channel().unwrap();
        store.save(config).unwrap();
        let saved: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(saved["unknown"]["keep"], 7);
        fs::write(&path, r#"{"version":1,"external":true}"#).unwrap();
        let previous = store.config().clone();
        assert_eq!(
            store.save(previous.clone()).unwrap_err().kind(),
            io::ErrorKind::AlreadyExists
        );
        assert_eq!(store.config(), &previous);
        assert_eq!(
            fs::read_to_string(path).unwrap(),
            r#"{"version":1,"external":true}"#
        );
    }
}
