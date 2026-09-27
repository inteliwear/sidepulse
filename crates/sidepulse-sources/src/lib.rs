//! Provider log discovery and bounded replay shared by the CLI and service.

mod discovery;
mod doctor;
mod tail;
mod transcript;

use std::env;
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

use sidepulse_core::{HookEvent, parse_log_line};

pub use discovery::discover_log_path;
pub use doctor::{ProviderConfig, detect_provider_configs};
pub use tail::SourceTailer;
pub use transcript::{is_transcript_provider, load_transcript_events};

pub const PROVIDERS: [&str; 5] = ["codex", "claude", "grok", "cursor", "junie"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceSpec {
    pub provider: String,
    pub path: PathBuf,
}

pub fn sources_from_environment(overrides: &[(String, PathBuf)]) -> Vec<SourceSpec> {
    let home = env::var_os("HOME")
        .or_else(|| env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let state_root = env::var_os("XDG_STATE_HOME").map(PathBuf::from);
    resolve_sources(&home, state_root.as_deref(), overrides)
}

pub fn resolve_sources(
    home: &Path,
    state_root: Option<&Path>,
    overrides: &[(String, PathBuf)],
) -> Vec<SourceSpec> {
    let state_root = state_root
        .map(|path| expand_home(path, home))
        .unwrap_or_else(|| home.join(".local/state"));
    let default_dir = state_root.join("sidepulse/agent-monitor");
    let mut sources = PROVIDERS
        .into_iter()
        .filter(|provider| {
            *provider != "cursor" || overrides.iter().any(|(name, _)| name == "cursor")
        })
        .map(|provider| {
            let path = overrides
                .iter()
                .rev()
                .find(|(name, _)| name == provider)
                .map(|(_, path)| path.clone())
                .or_else(|| discover_log_path(provider, home))
                .unwrap_or_else(|| default_dir.join(format!("{provider}.jsonl")));
            SourceSpec {
                provider: provider.to_owned(),
                path,
            }
        })
        .collect::<Vec<_>>();
    for (provider, path) in overrides {
        if !PROVIDERS.contains(&provider.as_str()) {
            sources.push(SourceSpec {
                provider: provider.clone(),
                path: path.clone(),
            });
        }
    }
    sources
}

fn expand_home(path: &Path, home: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    if text == "~" {
        home.to_path_buf()
    } else if let Some(suffix) = text.strip_prefix("~/") {
        home.join(suffix)
    } else {
        path.to_path_buf()
    }
}

pub fn load_recent_events(sources: &[SourceSpec], max_lines: usize) -> io::Result<Vec<HookEvent>> {
    let mut events = Vec::new();
    for source in sources {
        if is_transcript_provider(&source.provider) {
            events.extend(load_transcript_events(source)?);
            continue;
        }
        let mut file = match File::open(&source.path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(with_path(&source.path, error)),
        };
        let lines = read_recent_lines(&mut file, max_lines)
            .map_err(|error| with_path(&source.path, error))?;
        events.extend(
            lines
                .into_iter()
                .filter_map(|line| parse_log_line(&source.provider, &line)),
        );
    }
    events.sort_by_key(|event| event.logged_at);
    Ok(events)
}

/// Seek backward until every selected line has its start, leaving older log
/// bytes unread. The final partial line is retained for the JSON parser to
/// reject or accept, matching the previous forward reader.
fn read_recent_lines(file: &mut File, max_lines: usize) -> io::Result<Vec<String>> {
    if max_lines == 0 {
        return Ok(Vec::new());
    }
    let mut position = file.seek(SeekFrom::End(0))?;
    if position == 0 {
        return Ok(Vec::new());
    }
    file.seek(SeekFrom::Start(position - 1))?;
    let mut last = [0_u8; 1];
    file.read_exact(&mut last)?;
    let target_newlines = max_lines.saturating_add(usize::from(last[0] == b'\n'));
    let mut newline_count = 0;
    let mut chunks = Vec::new();
    while position > 0 && newline_count < target_newlines {
        let size = position.min(64 * 1024) as usize;
        position -= size as u64;
        file.seek(SeekFrom::Start(position))?;
        let mut chunk = vec![0; size];
        file.read_exact(&mut chunk)?;
        newline_count += chunk.iter().filter(|byte| **byte == b'\n').count();
        chunks.push(chunk);
    }
    let total = chunks.iter().map(Vec::len).sum();
    let mut bytes = Vec::with_capacity(total);
    for chunk in chunks.into_iter().rev() {
        bytes.extend(chunk);
    }
    let mut lines = bytes.split(|byte| *byte == b'\n').collect::<Vec<_>>();
    if bytes.last() == Some(&b'\n') {
        lines.pop();
    }
    let start = lines.len().saturating_sub(max_lines);
    lines[start..]
        .iter()
        .map(|line| {
            let line = line.strip_suffix(b"\r").unwrap_or(line);
            String::from_utf8(line.to_vec())
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
        })
        .collect()
}

fn with_path(path: &Path, error: io::Error) -> io::Error {
    io::Error::new(error.kind(), format!("{}: {error}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_defaults_and_explicit_paths() {
        let home = Path::new("/users/demo");
        let sources = resolve_sources(
            home,
            Some(Path::new("~/state")),
            &[("claude".into(), PathBuf::from("/logs/claude.jsonl"))],
        );
        assert_eq!(sources.len(), 4);
        assert_eq!(
            sources[0].path,
            home.join("state/sidepulse/agent-monitor/codex.jsonl")
        );
        assert_eq!(sources[1].path, PathBuf::from("/logs/claude.jsonl"));
        assert!(sources.iter().all(|source| source.provider != "cursor"));

        let with_cursor = resolve_sources(
            home,
            None,
            &[("cursor".into(), PathBuf::from("/logs/cursor.jsonl"))],
        );
        assert_eq!(with_cursor.len(), 5);
        assert_eq!(with_cursor[3].path, PathBuf::from("/logs/cursor.jsonl"));
    }

    #[test]
    fn bounded_tail_reads_complete_recent_lines_only() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("events.jsonl");
        let mut bytes = vec![b'x'; 200_000];
        bytes.extend_from_slice(b"\nfirst\nsecond\nthird\n");
        std::fs::write(&path, bytes).unwrap();
        assert_eq!(
            read_recent_lines(&mut File::open(&path).unwrap(), 2).unwrap(),
            ["second", "third"]
        );
        std::fs::write(&path, b"one\ntwo\nthree").unwrap();
        assert_eq!(
            read_recent_lines(&mut File::open(&path).unwrap(), 2).unwrap(),
            ["two", "three"]
        );
        assert!(
            read_recent_lines(&mut File::open(&path).unwrap(), 0)
                .unwrap()
                .is_empty()
        );
    }
}
