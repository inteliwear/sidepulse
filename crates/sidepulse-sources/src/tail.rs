use std::fs::{self, File, Metadata};
use std::io::{self, Read, Seek, SeekFrom};
use std::path::Path;

use sidepulse_core::HookEvent;

use crate::{
    SourceSpec, parse_log_line,
    transcript::{TranscriptCursor, is_transcript_provider},
};

const MAX_PENDING_BYTES: usize = 1024 * 1024;

struct Cursor {
    source: SourceSpec,
    offset: u64,
    identity: u128,
    pending: Vec<u8>,
    discarding: bool,
}

impl Cursor {
    fn new(source: SourceSpec) -> io::Result<Self> {
        let metadata = match fs::metadata(&source.path) {
            Ok(metadata) => Some(metadata),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(with_path(&source.path, error)),
        };
        Ok(Self {
            offset: metadata.as_ref().map_or(0, Metadata::len),
            identity: metadata.as_ref().map_or(0, file_identity),
            source,
            pending: Vec::new(),
            discarding: false,
        })
    }

    fn poll(&mut self, events: &mut Vec<HookEvent>) -> io::Result<()> {
        let mut file = match File::open(&self.source.path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                self.reset();
                return Ok(());
            }
            Err(error) => return Err(with_path(&self.source.path, error)),
        };
        let metadata = file
            .metadata()
            .map_err(|error| with_path(&self.source.path, error))?;
        let identity = file_identity(&metadata);
        if metadata.len() < self.offset || (self.identity != 0 && identity != self.identity) {
            self.reset();
        }
        self.identity = identity;
        file.seek(SeekFrom::Start(self.offset))
            .map_err(|error| with_path(&self.source.path, error))?;
        let mut buffer = [0_u8; 8192];
        loop {
            let count = file
                .read(&mut buffer)
                .map_err(|error| with_path(&self.source.path, error))?;
            if count == 0 {
                break;
            }
            self.offset += count as u64;
            for byte in &buffer[..count] {
                if *byte == b'\n' {
                    if !self.discarding
                        && let Ok(line) = std::str::from_utf8(&self.pending)
                        && let Some(event) = parse_log_line(&self.source.provider, line)
                    {
                        events.push(event);
                    }
                    self.pending.clear();
                    self.discarding = false;
                } else if !self.discarding {
                    if self.pending.len() < MAX_PENDING_BYTES {
                        self.pending.push(*byte);
                    } else {
                        self.pending.clear();
                        self.discarding = true;
                    }
                }
            }
        }
        Ok(())
    }

    fn reset(&mut self) {
        self.offset = 0;
        self.identity = 0;
        self.pending.clear();
        self.discarding = false;
    }
}

/// Capture current file ends before replay, then poll for new complete rows.
/// Rows appended during replay are safe to process twice; none are missed.
pub struct SourceTailer {
    cursors: Vec<Cursor>,
    transcripts: Vec<TranscriptCursor>,
}

impl SourceTailer {
    pub fn new(sources: &[SourceSpec]) -> io::Result<Self> {
        Ok(Self {
            cursors: sources
                .iter()
                .filter(|source| !is_transcript_provider(&source.provider))
                .cloned()
                .map(Cursor::new)
                .collect::<io::Result<Vec<_>>>()?,
            transcripts: sources
                .iter()
                .filter(|source| is_transcript_provider(&source.provider))
                .cloned()
                .map(TranscriptCursor::new)
                .collect::<io::Result<Vec<_>>>()?,
        })
    }

    pub fn poll(&mut self) -> io::Result<Vec<HookEvent>> {
        let mut events = Vec::new();
        for cursor in &mut self.cursors {
            cursor.poll(&mut events)?;
        }
        for cursor in &mut self.transcripts {
            cursor.poll(&mut events)?;
        }
        events.sort_by_key(|event| event.logged_at);
        Ok(events)
    }

    /// Reconfigure optional transcript sources without resetting JSONL cursors.
    pub fn sync_transcripts(&mut self, sources: &[SourceSpec]) -> io::Result<Vec<SourceSpec>> {
        let desired: Vec<_> = sources
            .iter()
            .filter(|source| is_transcript_provider(&source.provider))
            .collect();
        let added: Vec<_> = desired
            .iter()
            .filter(|source| {
                !self
                    .transcripts
                    .iter()
                    .any(|cursor| cursor.source() == **source)
            })
            .map(|source| (*source).clone())
            .collect();
        let new_cursors = added
            .iter()
            .cloned()
            .map(TranscriptCursor::new)
            .collect::<io::Result<Vec<_>>>()?;
        self.transcripts
            .retain(|cursor| desired.contains(&cursor.source()));
        self.transcripts.extend(new_cursors);
        Ok(added)
    }
}

#[cfg(unix)]
fn file_identity(metadata: &Metadata) -> u128 {
    use std::os::unix::fs::MetadataExt;

    (u128::from(metadata.dev()) << 64) | u128::from(metadata.ino())
}

#[cfg(not(unix))]
fn file_identity(metadata: &Metadata) -> u128 {
    metadata
        .created()
        .ok()
        .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
        .map_or(0, |duration| duration.as_nanos())
}

fn with_path(path: &Path, error: io::Error) -> io::Error {
    io::Error::new(error.kind(), format!("{}: {error}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn follows_append_partial_line_and_truncation() {
        let dir = std::env::temp_dir().join(format!(
            "sidepulse-tailer-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&dir).unwrap();
        let path = dir.join("claude.jsonl");
        fs::write(&path, b"old row\n").unwrap();
        let mut tailer = SourceTailer::new(&[SourceSpec {
            provider: "claude".into(),
            path: path.clone(),
        }])
        .unwrap();
        assert!(tailer.poll().unwrap().is_empty());
        let mut file = fs::OpenOptions::new().append(true).open(&path).unwrap();
        file.write_all(b"{\"logged_at\":\"2026-09-27T12:00:00Z\",\"hook_event_name\":\"PreToolUse\",\"session_id\":\"a\"}").unwrap();
        assert!(tailer.poll().unwrap().is_empty());
        file.write_all(b"\n").unwrap();
        assert_eq!(tailer.poll().unwrap()[0].event_name, "PreToolUse");
        drop(file);
        fs::write(&path, b"{\"logged_at\":\"2026-09-27T12:00:01Z\",\"hook_event_name\":\"Stop\",\"session_id\":\"a\"}\n").unwrap();
        assert_eq!(tailer.poll().unwrap()[0].event_name, "Stop");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn transcript_reconfiguration_keeps_jsonl_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("claude.jsonl");
        fs::write(&log, b"").unwrap();
        let source = SourceSpec {
            provider: "claude".into(),
            path: log.clone(),
        };
        let transcript = SourceSpec {
            provider: "codex-transcripts".into(),
            path: dir.path().join("sessions"),
        };
        let mut tailer = SourceTailer::new(std::slice::from_ref(&source)).unwrap();
        fs::write(&log, b"{\"logged_at\":\"2026-09-27T12:00:00Z\",\"hook_event_name\":\"PreToolUse\",\"session_id\":\"a\"}\n").unwrap();
        assert_eq!(
            tailer
                .sync_transcripts(&[source.clone(), transcript.clone()])
                .unwrap(),
            vec![transcript.clone()]
        );
        assert_eq!(tailer.poll().unwrap()[0].event_name, "PreToolUse");
        assert!(
            tailer
                .sync_transcripts(&[source.clone(), transcript.clone()])
                .unwrap()
                .is_empty()
        );
        assert!(
            tailer
                .sync_transcripts(std::slice::from_ref(&source))
                .unwrap()
                .is_empty()
        );
        assert!(tailer.transcripts.is_empty());
    }
}
