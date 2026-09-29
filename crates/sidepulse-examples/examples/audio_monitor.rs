use clap::Parser;
use sidepulse_examples::{AudioOptions, Output};
use std::{io, path::PathBuf};
#[cfg(feature = "live-audio")]
use std::{
    sync::atomic::Ordering,
    time::{Duration, Instant},
};
#[derive(Parser)]
#[command(about = "Smooth microphone volume as a green-to-red LED meter")]
struct Args {
    #[arg(long)]
    device: Option<PathBuf>,
    #[arg(long, default_value = "LEDS.LED")]
    file_name: String,
    #[arg(long)]
    led_count: Option<usize>,
    #[arg(long, default_value_t = 25.0)]
    fps: f64,
    #[arg(long, default_value_t = 90)]
    transition_ms: u32,
    #[arg(long, default_value_t = 0.08)]
    idle_brightness: f64,
    #[arg(long, default_value_t = 1.0)]
    max_brightness: f64,
    #[arg(long,default_value_t=-54.0,allow_hyphen_values=true)]
    noise_floor_db: f64,
    #[arg(long,default_value_t=-8.0,allow_hyphen_values=true)]
    peak_db: f64,
    #[arg(long, default_value_t = 0.0, allow_hyphen_values = true)]
    gain_db: f64,
    #[arg(long, default_value_t = 0.72)]
    curve: f64,
    #[arg(long, default_value_t = 0.045)]
    attack: f64,
    #[arg(long, default_value_t = 0.32)]
    release: f64,
    #[arg(long, default_value_t = 44100)]
    sample_rate: u32,
    #[arg(long, default_value_t = 0)]
    block_size: u32,
    #[arg(long)]
    input_device: Option<String>,
    #[arg(long)]
    list_inputs: bool,
    #[arg(long)]
    terminal: bool,
    #[arg(long)]
    dry_run: bool,
    #[arg(long)]
    off_on_exit: bool,
    /// Render one frame without opening an audio input.
    #[arg(long)]
    level: Option<f64>,
}
fn run(args: Args) -> io::Result<()> {
    if args.list_inputs {
        return list_inputs();
    }
    if !args.fps.is_finite() || args.sample_rate == 0 {
        return Err(io::Error::other(
            "fps must be finite and sample rate must be positive",
        ));
    }
    let options = AudioOptions {
        idle_brightness: args.idle_brightness,
        max_brightness: args.max_brightness,
        transition_ms: args.transition_ms,
        noise_floor_db: args.noise_floor_db,
        peak_db: args.peak_db,
        gain_db: args.gain_db,
        curve: args.curve,
        attack: args.attack,
        release: args.release,
    };
    options.validate()?;
    let mut output = Output::new(
        args.device.as_deref(),
        &args.file_name,
        args.led_count,
        args.dry_run,
        args.off_on_exit,
    )?;
    if let Some(level) = args.level {
        let program = options.program(level, output.count)?;
        output.send(&program)?;
        println!("{program}");
        return Ok(());
    }
    live(args, &options, &mut output)
}
#[cfg(not(feature = "live-audio"))]
fn list_inputs() -> io::Result<()> {
    Err(io::Error::other(
        "build with --features live-audio for microphone capture; --level renders offline",
    ))
}
#[cfg(not(feature = "live-audio"))]
fn live(_args: Args, _options: &AudioOptions, _output: &mut Output) -> io::Result<()> {
    list_inputs()
}
#[cfg(feature = "live-audio")]
fn list_inputs() -> io::Result<()> {
    use cpal::traits::{DeviceTrait, HostTrait};
    for (index, device) in cpal::default_host()
        .input_devices()
        .map_err(io::Error::other)?
        .enumerate()
    {
        let name = device.description().map_err(io::Error::other)?;
        let config = device.default_input_config().map_err(io::Error::other)?;
        println!(
            "{index}: {} ({} ch, {} Hz)",
            name.name(),
            config.channels(),
            config.sample_rate()
        );
    }
    Ok(())
}
#[cfg(feature = "live-audio")]
fn live(args: Args, options: &AudioOptions, output: &mut Output) -> io::Result<()> {
    use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
    use std::{
        collections::VecDeque,
        sync::{Arc, Mutex},
    };
    let host = cpal::default_host();
    let device = if let Some(wanted) = &args.input_device {
        host.input_devices()
            .map_err(io::Error::other)?
            .enumerate()
            .find_map(|(index, device)| {
                let found = wanted.parse::<usize>().map_or_else(
                    |_| {
                        device.description().is_ok_and(|description| {
                            description
                                .name()
                                .to_lowercase()
                                .contains(&wanted.to_lowercase())
                        })
                    },
                    |wanted| wanted == index,
                );
                found.then_some(device)
            })
    } else {
        host.default_input_device()
    }
    .ok_or_else(|| io::Error::other("no matching audio input device"))?;
    let default = device.default_input_config().map_err(io::Error::other)?;
    let supported = device
        .supported_input_configs()
        .map_err(io::Error::other)?
        .find(|config| {
            config.channels() == 1
                && config.min_sample_rate() <= args.sample_rate
                && config.max_sample_rate() >= args.sample_rate
        })
        .or_else(|| {
            device.supported_input_configs().ok()?.find(|config| {
                config.channels() == default.channels()
                    && config.min_sample_rate() <= args.sample_rate
                    && config.max_sample_rate() >= args.sample_rate
            })
        })
        .ok_or_else(|| {
            io::Error::other("audio input does not support the requested sample rate")
        })?;
    let supported = supported.with_sample_rate(args.sample_rate);
    let format = supported.sample_format();
    let mut config: cpal::StreamConfig = supported.into();
    if args.block_size > 0 {
        config.buffer_size = cpal::BufferSize::Fixed(args.block_size);
    }
    let samples = Arc::new(Mutex::new(VecDeque::<f64>::with_capacity(8)));
    let errors = Arc::new(Mutex::new(None::<String>));
    macro_rules! stream {
        ($sample:ty) => {{
            let samples = samples.clone();
            let errors = errors.clone();
            device
                .build_input_stream(
                    config,
                    move |data: &[$sample], _| {
                        use cpal::Sample;
                        if data.is_empty() {
                            return;
                        }
                        let rms = (data
                            .iter()
                            .map(|sample| sample.to_sample::<f64>().powi(2))
                            .sum::<f64>()
                            / data.len() as f64)
                            .sqrt();
                        if let Ok(mut samples) = samples.try_lock() {
                            if samples.len() == 8 {
                                samples.pop_front();
                            }
                            samples.push_back(rms);
                        }
                    },
                    move |error| {
                        if let Ok(mut latest) = errors.try_lock() {
                            *latest = Some(error.to_string().chars().take(1024).collect());
                        }
                    },
                    None,
                )
                .map_err(io::Error::other)?
        }};
    }
    let stream = match format {
        cpal::SampleFormat::F32 => stream!(f32),
        cpal::SampleFormat::F64 => stream!(f64),
        cpal::SampleFormat::I16 => stream!(i16),
        cpal::SampleFormat::I32 => stream!(i32),
        cpal::SampleFormat::U16 => stream!(u16),
        other => {
            return Err(io::Error::other(format!(
                "unsupported input sample format {other}"
            )));
        }
    };
    let stop = sidepulse_examples::stop_flag()?;
    stream.play().map_err(io::Error::other)?;
    let frame = Duration::from_secs_f64(1.0 / args.fps.clamp(1.0, 1000.0));
    let mut previous = Instant::now();
    let mut smoothed = 0.0;
    println!("Audio meter running; press Ctrl-C to stop.");
    while !stop.load(Ordering::Relaxed) {
        let start = Instant::now();
        let rms = samples
            .lock()
            .map_err(|_| io::Error::other("audio sample buffer lock failed"))?
            .drain(..)
            .fold(0.0_f64, f64::max);
        let now = Instant::now();
        smoothed = options.smooth(smoothed, options.level(rms), (now - previous).as_secs_f64());
        previous = now;
        output.send(&options.program(smoothed, output.count)?)?;
        if args.terminal || args.dry_run {
            println!(
                "{:5.1}% {:6.1} dBFS",
                smoothed * 100.0,
                sidepulse_examples::dbfs(rms)
            );
        }
        if let Some(error) = errors
            .lock()
            .map_err(|_| io::Error::other("audio error buffer lock failed"))?
            .take()
        {
            eprintln!("audio stream: {error}");
        }
        sidepulse_examples::wait(&stop, frame.saturating_sub(start.elapsed()));
    }
    drop(stream);
    Ok(())
}
fn main() -> std::process::ExitCode {
    match run(Args::parse()) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("audio monitor: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}
