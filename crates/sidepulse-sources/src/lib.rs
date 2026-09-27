//! Provider log discovery and bounded replay shared by the CLI and service.

mod discovery;
mod doctor;
mod tail;
mod transcript;

use std::collections::VecDeque;
use std::env;
use std::fs::File;
use std::io::{self, BufRead, BufReader};
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
        let file = match File::open(&source.path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(with_path(&source.path, error)),
        };
        let mut lines = VecDeque::with_capacity(max_lines.min(5000));
        for line in BufReader::new(file).lines() {
            let line = line.map_err(|error| with_path(&source.path, error))?;
            if max_lines == 0 {
                continue;
            }
            if lines.len() == max_lines {
                lines.pop_front();
            }
            lines.push_back(line);
        }
        events.extend(
            lines
                .into_iter()
                .filter_map(|line| parse_log_line(&source.provider, &line)),
        );
    }
    events.sort_by_key(|event| event.logged_at);
    Ok(events)
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
        assert_eq!(sources.len(), 5);
        assert_eq!(
            sources[0].path,
            home.join("state/sidepulse/agent-monitor/codex.jsonl")
        );
        assert_eq!(sources[1].path, PathBuf::from("/logs/claude.jsonl"));
    }
}
