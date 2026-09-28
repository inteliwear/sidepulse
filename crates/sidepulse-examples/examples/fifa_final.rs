//! Demonstration teams and source URL retained from the original example.
use clap::Parser;
use sidepulse_examples::{Output, Score, find_match, score_program};
use std::{
    io::{self, Read},
    path::PathBuf,
    sync::atomic::Ordering,
    time::Duration,
};
#[derive(Parser)]
#[command(about = "Display the original Spain/Argentina score demonstration on LEDs")]
struct Args {
    #[arg(long)]
    device: Option<PathBuf>,
    #[arg(long, default_value = "LEDS.LED")]
    file_name: String,
    #[arg(long)]
    led_count: Option<usize>,
    #[arg(long, default_value_t = 30.0)]
    poll: f64,
    #[arg(long, default_value_t = 0.012)]
    dim: f64,
    #[arg(long, default_value_t = 250)]
    transition_ms: u32,
    #[arg(long)]
    score: Option<String>,
    #[arg(long)]
    once: bool,
    #[arg(long)]
    dry_run: bool,
    #[arg(long)]
    off_on_exit: bool,
    /// Read a captured scoreboard instead of fetching live data.
    #[arg(long)]
    scoreboard_file: Option<PathBuf>,
    #[arg(
        long,
        default_value = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
    )]
    url: String,
}
fn read_scoreboard(args: &Args) -> io::Result<serde_json::Value> {
    let reader: Box<dyn Read> = if let Some(file) = &args.scoreboard_file {
        Box::new(std::fs::File::open(file)?)
    } else {
        Box::new(
            ureq::AgentBuilder::new()
                .timeout(Duration::from_secs(15))
                .build()
                .get(&args.url)
                .set("User-Agent", "sidepulse-fifa-final/1.0")
                .call()
                .map_err(io::Error::other)?
                .into_reader(),
        )
    };
    let mut bytes = Vec::new();
    reader.take(1024 * 1024 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > 1024 * 1024 {
        return Err(io::Error::other("scoreboard exceeds 1 MiB"));
    }
    serde_json::from_slice(&bytes).map_err(io::Error::other)
}
fn run(args: Args) -> io::Result<bool> {
    if !args.poll.is_finite() || args.poll > 86400.0 || !args.dim.is_finite() {
        return Err(io::Error::other("poll and dim must be finite"));
    }
    let fixed = if let Some(score) = &args.score {
        let (left, right) = score
            .split_once('-')
            .ok_or_else(|| io::Error::other("expected SPAIN-ARGENTINA goals, e.g. 2-1"))?;
        Some((
            left.parse::<i32>().map_err(io::Error::other)?,
            right.parse::<i32>().map_err(io::Error::other)?,
        ))
    } else {
        None
    };
    let stop = sidepulse_examples::stop_flag()?;
    let mut output = Output::new(
        args.device.as_deref(),
        &args.file_name,
        args.led_count,
        args.dry_run,
        args.off_on_exit,
    )?;
    let mut last = None;
    while !stop.load(Ordering::Relaxed) {
        let score = if let Some((left, right)) = fixed {
            Some(Score {
                left,
                right,
                state: "in".into(),
                detail: "manual score".into(),
            })
        } else {
            match read_scoreboard(&args) {
                Ok(data) => find_match(&data),
                Err(error) => {
                    eprintln!("scoreboard fetch failed: {error}");
                    None
                }
            }
        };
        if let Some(score) = score {
            if let Some((left, right)) = last
                && (left, right) != (score.left, score.right)
                && !args.dry_run
            {
                output.send(&sidepulse_examples::celebration(score.left > left))?;
                sidepulse_examples::wait(&stop, Duration::from_secs_f64(3.6));
                if stop.load(Ordering::Relaxed) {
                    break;
                }
            }
            let program = score_program(
                score.left,
                score.right,
                output.count,
                args.dim,
                args.transition_ms,
            )?;
            output.send(&program)?;
            if last != Some((score.left, score.right)) {
                println!(
                    "Spain {} - {} Argentina ({})\n{program}",
                    score.left, score.right, score.detail
                );
            }
            last = Some((score.left, score.right));
            if score.state == "post" || args.once {
                return Ok(true);
            }
        } else {
            eprintln!("no Spain vs Argentina match on the scoreboard");
            if args.once {
                return Ok(false);
            }
        }
        sidepulse_examples::wait(&stop, Duration::from_secs_f64(args.poll.max(5.0)));
    }
    Ok(true)
}
fn main() -> std::process::ExitCode {
    match run(Args::parse()) {
        Ok(true) => std::process::ExitCode::SUCCESS,
        Ok(false) => std::process::ExitCode::FAILURE,
        Err(error) => {
            eprintln!("score display: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}
