use std::collections::HashMap;
use std::env;
use std::io::{self, Write};
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use sidepulse_core::{AgentStatus, Monitor, MonitorSnapshot, MonitoringPolicy};
use sidepulse_sources::{PROVIDERS, SourceSpec, load_recent_events, sources_from_environment};

struct Options {
    json: bool,
    include_stale: bool,
    policy: MonitoringPolicy,
    max_lines: usize,
    logs: HashMap<&'static str, PathBuf>,
    transcripts: HashMap<&'static str, PathBuf>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            json: false,
            include_stale: false,
            policy: MonitoringPolicy::default(),
            max_lines: 5000,
            logs: HashMap::new(),
            transcripts: HashMap::new(),
        }
    }
}

pub fn run_status(args: impl Iterator<Item = String>) -> ExitCode {
    let options = match parse_args(args) {
        Ok(options) => options,
        Err(message) => {
            eprintln!("sidepulse-next status: {message}");
            return ExitCode::from(2);
        }
    };
    let (snapshot, sources) = match collect_snapshot(&options) {
        Ok(result) => result,
        Err(error) => {
            eprintln!("sidepulse-next status: {error}");
            return ExitCode::FAILURE;
        }
    };
    if options.json {
        let output = snapshot_json(&snapshot, &sources);
        println!(
            "{}",
            serde_json::to_string_pretty(&output).expect("status JSON serializes")
        );
    } else {
        print_snapshot(&snapshot, &sources, options.include_stale, None);
    }
    ExitCode::SUCCESS
}

fn collect_snapshot(options: &Options) -> io::Result<(MonitorSnapshot, Vec<SourceSpec>)> {
    let sources = sources_for_options(options);
    let events = load_recent_events(&sources, options.max_lines)?;
    let mut monitor = Monitor::new(options.policy);
    for event in &events {
        monitor.ingest(event);
    }
    let snapshot = monitor.snapshot(Utc::now());
    Ok((snapshot, sources))
}

fn sources_for_options(options: &Options) -> Vec<SourceSpec> {
    let mut overrides = options
        .logs
        .iter()
        .map(|(provider, path)| (provider.to_string(), path.clone()))
        .collect::<Vec<_>>();
    overrides.extend(
        options
            .transcripts
            .iter()
            .map(|(provider, path)| (format!("{provider}-transcripts"), path.clone())),
    );
    sources_from_environment(&overrides)
}
pub(crate) struct DetachedMonitor {
    monitor: Monitor,
    tailer: sidepulse_sources::SourceTailer,
}
impl DetachedMonitor {
    pub(crate) fn new(args: Vec<String>) -> io::Result<Self> {
        let options = parse_args(args.into_iter()).map_err(io::Error::other)?;
        let sources = sources_for_options(&options);
        let tailer = sidepulse_sources::SourceTailer::new(&sources)?;
        let mut monitor = Monitor::new(options.policy);
        for event in load_recent_events(&sources, options.max_lines)? {
            monitor.ingest(&event);
        }
        Ok(Self { monitor, tailer })
    }
    pub(crate) fn snapshot(&mut self) -> io::Result<MonitorSnapshot> {
        for event in self.tailer.poll()? {
            self.monitor.ingest(&event);
        }
        Ok(self.monitor.snapshot(Utc::now()))
    }
}

pub fn run_watch(args: impl Iterator<Item = String>) -> ExitCode {
    let mut interval = 1.0;
    let mut recent_seconds = 3600.0;
    let mut status_args = Vec::new();
    let mut args = args;
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--interval" | "--recent-seconds" => {
                let Some(value) = args.next() else {
                    eprintln!("sidepulse-next watch: {flag} needs a value");
                    return ExitCode::from(2);
                };
                let Ok(number) = value.parse::<f64>() else {
                    eprintln!("sidepulse-next watch: invalid {flag}: {value}");
                    return ExitCode::from(2);
                };
                if !number.is_finite() || number <= 0.0 || (flag == "--interval" && number > 3600.0)
                {
                    eprintln!("sidepulse-next watch: invalid {flag}: {value}");
                    return ExitCode::from(2);
                }
                if flag == "--interval" {
                    interval = number;
                } else {
                    recent_seconds = number;
                }
            }
            "--no-color" => {}
            _ => status_args.push(flag),
        }
    }
    let options = match parse_args(status_args.into_iter()) {
        Ok(options) if !options.json => options,
        Ok(_) => {
            eprintln!("sidepulse-next watch: --json is not supported");
            return ExitCode::from(2);
        }
        Err(message) => {
            eprintln!("sidepulse-next watch: {message}");
            return ExitCode::from(2);
        }
    };
    loop {
        match collect_snapshot(&options) {
            Ok((snapshot, sources)) => {
                print!("\x1b[2J\x1b[H");
                print_snapshot(
                    &snapshot,
                    &sources,
                    options.include_stale,
                    Some(recent_seconds),
                );
                let _ = io::stdout().flush();
            }
            Err(error) => eprintln!("sidepulse-next watch: {error}"),
        }
        std::thread::sleep(Duration::from_secs_f64(interval));
    }
}

fn parse_args(args: impl Iterator<Item = String>) -> Result<Options, String> {
    let mut options = Options::default();
    let mut args = args.peekable();
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--json" => options.json = true,
            "--all" => options.include_stale = true,
            "--stale-after" | "--tool-running-timeout" => {
                let value = args.next().ok_or_else(|| format!("{flag} needs a value"))?;
                let number = value
                    .parse::<f64>()
                    .map_err(|_| format!("invalid {flag}: {value}"))?;
                if !number.is_finite() {
                    return Err(format!("invalid {flag}: {value}"));
                }
                if flag == "--stale-after" {
                    options.policy.stale_after_seconds = number;
                } else {
                    options.policy.tool_running_timeout_seconds = number;
                }
            }
            "--max-lines" => {
                let value = args.next().ok_or("--max-lines needs a value")?;
                options.max_lines = value
                    .parse::<usize>()
                    .map_err(|_| format!("invalid --max-lines: {value}"))?;
            }
            _ => {
                if flag == "--codex-transcripts" || flag == "--claude-transcripts" {
                    let provider = if flag == "--codex-transcripts" {
                        "codex"
                    } else {
                        "claude"
                    };
                    let value = args
                        .next()
                        .ok_or_else(|| format!("{flag} needs a directory"))?;
                    options.transcripts.insert(provider, expand_home(&value));
                    continue;
                }
                let provider = PROVIDERS
                    .iter()
                    .copied()
                    .find(|provider| flag == format!("--{provider}-log"));
                if let Some(provider) = provider {
                    let value = args.next().ok_or_else(|| format!("{flag} needs a path"))?;
                    options.logs.insert(provider, expand_home(&value));
                } else {
                    return Err(format!("unknown argument: {flag}"));
                }
            }
        }
    }
    Ok(options)
}

fn expand_home(value: &str) -> PathBuf {
    if (value == "~" || value.starts_with("~/"))
        && let Some(home) = env::var_os("HOME").or_else(|| env::var_os("USERPROFILE"))
    {
        return PathBuf::from(home).join(value.strip_prefix("~/").unwrap_or(""));
    }
    PathBuf::from(value)
}

fn snapshot_json(snapshot: &MonitorSnapshot, sources: &[SourceSpec]) -> Value {
    let now = snapshot.collected_at;
    json!({
        "collected_at": now.to_rfc3339(),
        "sources": sources.iter().map(|source| json!({"provider": source.provider, "path": source.path.to_string_lossy()})).collect::<Vec<_>>(),
        "aggregate": {
            "mode": snapshot.aggregate.mode,
            "mode_label": snapshot.aggregate.mode.label(),
            "active_count": snapshot.aggregate.active_count,
            "stale_count": snapshot.aggregate.stale_count,
            "representative": snapshot.aggregate.representative.as_ref().map(|status| status.legacy_json(now)),
        },
        "statuses": snapshot.statuses.iter().map(|status| status.legacy_json(now)).collect::<Vec<_>>(),
        "stale_statuses": snapshot.stale_statuses.iter().map(|status| status.legacy_json(now)).collect::<Vec<_>>(),
    })
}

fn describe_status(status: &AgentStatus, now: DateTime<Utc>) -> String {
    let mut text = format!(
        "{}: {} event={}",
        status.display_name,
        status.mode.label(),
        status.event_name
    );
    if let Some(origin) = &status.origin {
        text.push_str(&format!(" origin={origin}"));
    }
    if let Some(tool) = &status.tool_name {
        text.push_str(&format!(" tool={tool}"));
    }
    text.push_str(&format!(" age={}s", status.age_seconds(now) as u64));
    if status.stale {
        text.push_str(" stale");
    }
    if let Some(cwd) = &status.cwd {
        text.push_str(&format!(" cwd={cwd}"));
    }
    text
}

fn print_snapshot(
    snapshot: &MonitorSnapshot,
    sources: &[SourceSpec],
    include_stale: bool,
    recent_seconds: Option<f64>,
) {
    println!(
        "Aggregate: {} ({} active, {} stale)",
        snapshot.aggregate.mode.label(),
        snapshot.aggregate.active_count,
        snapshot.aggregate.stale_count
    );
    if let Some(status) = &snapshot.aggregate.representative {
        println!("Reason: {}", describe_status(status, snapshot.collected_at));
    }
    println!("\nSources:");
    for source in sources {
        println!(
            "  {}: {} [{}]",
            source.provider,
            source.path.display(),
            if source.path.exists() {
                "ok"
            } else {
                "missing"
            }
        );
    }
    println!("\nAgents:");
    let statuses = snapshot.statuses.iter().chain(if include_stale {
        snapshot.stale_statuses.iter()
    } else {
        [].iter()
    });
    let mut any = false;
    for status in statuses {
        if !include_stale
            && recent_seconds
                .is_some_and(|seconds| status.age_seconds(snapshot.collected_at) > seconds)
        {
            continue;
        }
        any = true;
        println!("  {}", describe_status(status, snapshot.collected_at));
    }
    if !any {
        println!("  none");
    }
}
