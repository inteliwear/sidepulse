use std::collections::HashMap;
use std::env;
use std::io::{self, IsTerminal, Write};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use sidepulse_core::{AgentStatus, Monitor, MonitorSnapshot, MonitoringPolicy};
use sidepulse_sources::{PROVIDERS, SourceSpec, load_recent_events, sources_from_environment};

struct Options {
    help: bool,
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
            help: false,
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
    if options.help {
        return print_help(false);
    }
    let (snapshot, sources) = match collect_snapshot(&options) {
        Ok(result) => result,
        Err(error) => {
            eprintln!("sidepulse-next status: {error}");
            return ExitCode::FAILURE;
        }
    };
    let mut output = io::stdout().lock();
    let result = if options.json {
        let document = snapshot_json(&snapshot, &sources);
        writeln!(
            output,
            "{}",
            serde_json::to_string_pretty(&document).expect("status JSON serializes")
        )
    } else {
        write_snapshot(
            &mut output,
            &snapshot,
            &sources,
            options.include_stale,
            None,
        )
    };
    output_exit(result.and_then(|()| output.flush()), "status")
}

fn output_exit(result: io::Result<()>, command: &str) -> ExitCode {
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) if error.kind() == io::ErrorKind::BrokenPipe => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("sidepulse-next {command}: {error}");
            ExitCode::FAILURE
        }
    }
}

fn print_help(live: bool) -> ExitCode {
    let mut output = io::stdout().lock();
    let result = (|| -> io::Result<()> {
        writeln!(
            output,
            "Usage: sidepulse agent-monitor {} [OPTIONS]",
            if live { "live|watch" } else { "status" }
        )?;
        writeln!(
            output,
            "\nSources:\n  --codex-log PATH, --claude-log PATH, --grok-log PATH\n  --cursor-log PATH, --junie-log PATH\n  --codex-transcripts DIR, --claude-transcripts DIR\n\nMonitoring:\n  --max-lines COUNT              Initial replay limit (default: 5000)\n  --stale-after SECONDS          Agent inactivity timeout\n  --tool-running-timeout SECONDS Tool-running timeout\n  --all                         Include stale agents"
        )?;
        if live {
            writeln!(
                output,
                "\nLive display:\n  --interval SECONDS             Refresh interval (default: 1)\n  --recent-seconds SECONDS       Display age limit (default: 3600; 0 disables)\n  --no-color                     Plain text output\n\nPress Ctrl-C to stop. A closed output pipe also stops monitoring."
            )?;
        } else {
            writeln!(
                output,
                "  --json                        Print the status JSON document"
            )?;
        }
        writeln!(output, "\n  -h, --help                    Show this help")?;
        output.flush()
    })();
    output_exit(result, "help")
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
    sources: Vec<SourceSpec>,
}
impl DetachedMonitor {
    pub(crate) fn new(args: Vec<String>) -> io::Result<Self> {
        let options = parse_args(args.into_iter()).map_err(io::Error::other)?;
        Self::from_options(&options)
    }
    fn from_options(options: &Options) -> io::Result<Self> {
        let sources = sources_for_options(options);
        let tailer = sidepulse_sources::SourceTailer::new(&sources)?;
        let mut monitor = Monitor::new(options.policy);
        for event in load_recent_events(&sources, options.max_lines)? {
            monitor.ingest(&event);
        }
        Ok(Self {
            monitor,
            tailer,
            sources,
        })
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
            "--help" | "-h" => return print_help(true),
            "--interval" | "--recent-seconds" => {
                let Some(value) = args.next() else {
                    eprintln!("sidepulse-next watch: {flag} needs a value");
                    return ExitCode::from(2);
                };
                let Ok(number) = value.parse::<f64>() else {
                    eprintln!("sidepulse-next watch: invalid {flag}: {value}");
                    return ExitCode::from(2);
                };
                if !number.is_finite()
                    || number < 0.0
                    || (flag == "--interval" && (number == 0.0 || number > 3600.0))
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
    let mut monitor = match DetachedMonitor::from_options(&options) {
        Ok(monitor) => monitor,
        Err(error) => {
            eprintln!("sidepulse-next watch: {error}");
            return ExitCode::FAILURE;
        }
    };
    let stop = Arc::new(AtomicBool::new(false));
    let signal = stop.clone();
    if let Err(error) = ctrlc::set_handler(move || signal.store(true, Ordering::Release)) {
        eprintln!("sidepulse-next watch: {error}");
        return ExitCode::FAILURE;
    }
    let stdout = io::stdout();
    let interactive = stdout.is_terminal();
    while !stop.load(Ordering::Acquire) {
        match monitor.snapshot() {
            Ok(snapshot) => {
                let mut output = stdout.lock();
                let result = (|| -> io::Result<()> {
                    if interactive {
                        write!(output, "\x1b[2J\x1b[H")?;
                    }
                    write_snapshot(
                        &mut output,
                        &snapshot,
                        &monitor.sources,
                        options.include_stale,
                        Some(recent_seconds),
                    )?;
                    output.flush()
                })();
                if result.is_err() {
                    return output_exit(result, "watch");
                }
            }
            Err(error) => eprintln!("sidepulse-next watch: {error}"),
        }
        let next = Instant::now() + Duration::from_secs_f64(interval);
        while !stop.load(Ordering::Acquire) && Instant::now() < next {
            std::thread::sleep(
                next.saturating_duration_since(Instant::now())
                    .min(Duration::from_millis(50)),
            );
        }
    }
    ExitCode::SUCCESS
}

fn parse_args(args: impl Iterator<Item = String>) -> Result<Options, String> {
    let mut options = Options::default();
    let mut args = args.peekable();
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--help" | "-h" => {
                options.help = true;
                break;
            }
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

fn write_snapshot(
    output: &mut impl Write,
    snapshot: &MonitorSnapshot,
    sources: &[SourceSpec],
    include_stale: bool,
    recent_seconds: Option<f64>,
) -> io::Result<()> {
    writeln!(
        output,
        "Aggregate: {} ({} active, {} stale)",
        snapshot.aggregate.mode.label(),
        snapshot.aggregate.active_count,
        snapshot.aggregate.stale_count
    )?;
    if let Some(status) = &snapshot.aggregate.representative {
        writeln!(
            output,
            "Reason: {}",
            describe_status(status, snapshot.collected_at)
        )?;
    }
    writeln!(output, "\nSources:")?;
    for source in sources {
        writeln!(
            output,
            "  {}: {} [{}]",
            source.provider,
            source.path.display(),
            if source.path.exists() {
                "ok"
            } else {
                "missing"
            }
        )?;
    }
    writeln!(output, "\nAgents:")?;
    let statuses = snapshot.statuses.iter().chain(if include_stale {
        snapshot.stale_statuses.iter()
    } else {
        [].iter()
    });
    let mut any = false;
    for status in statuses {
        if !include_stale
            && recent_seconds.is_some_and(|seconds| {
                seconds > 0.0 && status.age_seconds(snapshot.collected_at) > seconds
            })
        {
            continue;
        }
        any = true;
        writeln!(
            output,
            "  {}",
            describe_status(status, snapshot.collected_at)
        )?;
    }
    if !any {
        writeln!(output, "  none")?;
    }
    Ok(())
}
