//! Read-only log inspection and explicit atomic exports, independent of the UI.
use sidepulse_core::{DiagnosticFormat, DiagnosticsStatus, ServerPayload};
use std::{
    fs,
    io::{self, BufReader, Read, Write},
    path::{Path, PathBuf},
};

#[derive(Clone)]
pub(super) struct Diagnostics {
    settings: Option<PathBuf>,
    audits: Vec<PathBuf>,
    history: Option<PathBuf>,
    exports: PathBuf,
}

impl super::Service {
    pub fn configure_diagnostics(
        &self,
        audit: &Path,
        settings: Option<&Path>,
        history: Option<&Path>,
        exports: &Path,
    ) -> io::Result<()> {
        self.configure_diagnostic_sources(&[audit.into()], settings, history, exports)
    }

    pub fn configure_diagnostic_sources(
        &self,
        audits: &[PathBuf],
        settings: Option<&Path>,
        history: Option<&Path>,
        exports: &Path,
    ) -> io::Result<()> {
        *self.diagnostics.lock().map_err(super::poisoned)? = Some(Diagnostics {
            settings: settings.map(Path::to_path_buf),
            audits: audits.to_vec(),
            history: history.map(Path::to_path_buf),
            exports: exports.into(),
        });
        Ok(())
    }

    pub fn diagnostics_status(&self) -> io::Result<DiagnosticsStatus> {
        let config = self.diagnostics.lock().map_err(super::poisoned)?;
        Ok(config.as_ref().map_or_else(
            || DiagnosticsStatus {
                settings_path: None,
                audit_path: None,
                audit_paths: vec![],
                audit_bytes: 0,
                history_path: None,
                export_directory: None,
            },
            |config| DiagnosticsStatus {
                settings_path: config
                    .settings
                    .as_ref()
                    .map(|path| path.to_string_lossy().into_owned()),
                audit_path: config
                    .audits
                    .first()
                    .map(|path| path.to_string_lossy().into_owned()),
                audit_paths: config
                    .audits
                    .iter()
                    .map(|path| path.to_string_lossy().into_owned())
                    .collect(),
                audit_bytes: config
                    .audits
                    .iter()
                    .map(|path| fs::metadata(path).map_or(0, |metadata| metadata.len()))
                    .sum(),
                history_path: config
                    .history
                    .as_ref()
                    .map(|path| path.to_string_lossy().into_owned()),
                export_directory: Some(config.exports.to_string_lossy().into_owned()),
            },
        ))
    }

    pub fn export_diagnostics(&self, format: DiagnosticFormat) -> io::Result<ServerPayload> {
        let config = self
            .diagnostics
            .lock()
            .map_err(super::poisoned)?
            .clone()
            .ok_or_else(|| io::Error::other("debug exports are unavailable in this service"))?;
        let mut reader: Box<dyn io::BufRead> = Box::new(io::Cursor::new(Vec::<u8>::new()));
        let mut total = 0;
        for path in &config.audits {
            let file = match fs::File::open(path) {
                Ok(file) => file,
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error),
            };
            let bytes = file.metadata()?.len();
            total += bytes;
            if total > 128 * 1024 * 1024 {
                return Err(io::Error::other(
                    "debug logs exceed the 128 MiB export limit",
                ));
            }
            reader = Box::new(
                reader
                    .chain(BufReader::new(file.take(bytes)))
                    .chain(io::Cursor::new(vec![b'\n'])),
            );
        }
        fs::create_dir_all(&config.exports)?;
        let mut output = tempfile::NamedTempFile::new_in(&config.exports)?;
        let events = sidepulse_core::export_status_audit(reader, &mut output, format)?;
        output.flush()?;
        output.as_file().sync_all()?;
        let suffix = match format {
            DiagnosticFormat::Csv => "csv",
            DiagnosticFormat::Html => "html",
        };
        let path = config.exports.join(format!(
            "sidepulse-debug-{}-{}.{}",
            chrono::Utc::now().format("%Y%m%d-%H%M%S"),
            uuid::Uuid::new_v4(),
            suffix
        ));
        output
            .persist_noclobber(&path)
            .map_err(|error| error.error)?;
        Ok(ServerPayload::DiagnosticsExported {
            path: path.to_string_lossy().into_owned(),
            events,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exports_are_atomic_unique_and_do_not_modify_sources() {
        let temp = tempfile::tempdir().unwrap();
        let audit = temp.path().join("event-status.jsonl");
        let bytes = b"{\"provider\":\"codex\",\"message\":\"hi\"}\ninvalid\n";
        fs::write(&audit, bytes).unwrap();
        let service = super::super::Service::new();
        service
            .configure_diagnostics(&audit, None, None, &temp.path().join("exports"))
            .unwrap();
        let first = service.export_diagnostics(DiagnosticFormat::Csv).unwrap();
        let ServerPayload::DiagnosticsExported {
            path: first,
            events: 1,
        } = first
        else {
            panic!("missing export");
        };
        let ServerPayload::DiagnosticsExported {
            path: second,
            events: 1,
        } = service.export_diagnostics(DiagnosticFormat::Html).unwrap()
        else {
            panic!("missing HTML export");
        };
        assert_ne!(first, second);
        assert!(fs::read_to_string(first).unwrap().contains("codex"));
        assert!(fs::read_to_string(second).unwrap().contains("<table>"));
        assert_eq!(fs::read(audit).unwrap(), bytes);
        assert_eq!(
            fs::read_dir(temp.path().join("exports")).unwrap().count(),
            2
        );
    }

    #[test]
    fn exports_combine_sources_with_unterminated_rows_and_discard_failed_output() {
        let temp = tempfile::tempdir().unwrap();
        let a = temp.path().join("a.jsonl");
        let b = temp.path().join("b.jsonl");
        fs::write(&a, br#"{"provider":"codex"}"#).unwrap();
        fs::write(&b, b"{\"provider\":\"claude\"}\n").unwrap();
        let exports = temp.path().join("exports");
        let service = super::super::Service::new();
        service
            .configure_diagnostic_sources(&[a.clone(), b.clone()], None, None, &exports)
            .unwrap();
        let ServerPayload::DiagnosticsExported { path, events: 2 } =
            service.export_diagnostics(DiagnosticFormat::Csv).unwrap()
        else {
            panic!("missing combined export");
        };
        let csv = fs::read_to_string(path).unwrap();
        assert_eq!(csv.matches("audited_at,logged_at").count(), 1);
        assert!(csv.contains("codex") && csv.contains("claude"));
        fs::write(&b, vec![b'x'; 1_048_577]).unwrap();
        assert!(service.export_diagnostics(DiagnosticFormat::Html).is_err());
        assert_eq!(fs::read_dir(exports).unwrap().count(), 1);
    }
}
