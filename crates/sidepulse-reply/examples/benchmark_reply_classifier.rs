//! Run with `cargo run --release -p sidepulse-reply --example
//! benchmark_reply_classifier -- --cache-dir PATH --warm-runs 5`.
use serde_json::json;
use sidepulse_reply::{ReplyClassifier, model_files};
use std::{fs, io, path::PathBuf, time::Instant};

const CANONICAL: [(&str, &str); 8] = [
    ("Can you send the document?", "REPLY_REQUIRED"),
    ("Please send the document.", "REPLY_REQUIRED"),
    ("Let me know when you arrive.", "REPLY_REQUIRED"),
    ("Which option should we choose?", "REPLY_REQUIRED"),
    ("Thanks, I received it.", "NO_REPLY_REQUIRED"),
    (
        "Just an FYI, the deployment is complete.",
        "NO_REPLY_REQUIRED",
    ),
    ("Sounds good.", "NO_REPLY_REQUIRED"),
    ("Have a good weekend.", "NO_REPLY_REQUIRED"),
];

fn run() -> io::Result<()> {
    let mut cache = model_files::default_cache()?;
    let mut model = None;
    let mut tokenizer = None;
    let mut log = None;
    let mut log_limit = 12;
    let mut warm_runs = 20;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        let value = args
            .next()
            .ok_or_else(|| io::Error::other(format!("{arg} requires a value")))?;
        match arg.as_str() {
            "--cache-dir" => cache = value.into(),
            "--model" => model = Some(PathBuf::from(value)),
            "--tokenizer" => tokenizer = Some(PathBuf::from(value)),
            "--log" => log = Some(PathBuf::from(value)),
            "--log-examples" => log_limit = value.parse::<usize>().map_err(io::Error::other)?,
            "--warm-runs" => warm_runs = value.parse::<usize>().map_err(io::Error::other)?,
            _ => return Err(io::Error::other(format!("unknown option {arg}"))),
        }
    }
    if warm_runs > 1000 || log_limit > 1000 {
        return Err(io::Error::other("benchmark counts must be at most 1000"));
    }
    let (model, tokenizer) = match (model, tokenizer) {
        (Some(model), Some(tokenizer)) => (model, tokenizer),
        (None, None) => model_files::cached_default(&cache)?,
        _ => {
            return Err(io::Error::other(
                "custom models require --model and --tokenizer",
            ));
        }
    };
    let before = Instant::now();
    let mut classifier = ReplyClassifier::load(&model, &tokenizer)?;
    let load_seconds = before.elapsed().as_secs_f64();
    let mut examples: Vec<(String, Option<&str>, &str)> = CANONICAL
        .iter()
        .map(|(text, expected)| (text.to_string(), Some(*expected), "canonical"))
        .collect();
    if let Some(path) = &log {
        if fs::metadata(path)?.len() > 64 * 1024 * 1024 {
            return Err(io::Error::other("benchmark log exceeds 64 MiB"));
        }
        let mut messages = Vec::new();
        for line in fs::read_to_string(path)?.lines() {
            let Ok(event) = serde_json::from_str::<serde_json::Value>(line) else {
                continue;
            };
            if event["hook_event"] == "Stop"
                && let Some(message) = event["message"].as_str()
            {
                let message = message.split_whitespace().collect::<Vec<_>>().join(" ");
                if !message.is_empty() && !messages.contains(&message) {
                    messages.push(message);
                }
            }
        }
        let skip = messages.len().saturating_sub(log_limit);
        examples.extend(
            messages
                .into_iter()
                .skip(skip)
                .map(|text| (text, None, "event-status.jsonl")),
        );
    }
    let mut rows = Vec::new();
    let mut counts = json!({"REPLY_REQUIRED":0,"NO_REPLY_REQUIRED":0,"UNCERTAIN":0});
    let mut correct = 0;
    for (message, expected, source) in examples {
        let before = Instant::now();
        let result = classifier.classify(&message)?;
        let latency_ms = before.elapsed().as_secs_f64() * 1000.0;
        if expected == Some(result.label.as_str()) {
            correct += 1;
        }
        counts[result.label.as_str()] = json!(counts[result.label.as_str()].as_u64().unwrap() + 1);
        rows.push(
            json!({"source":source,"message":message,"expected":expected,
            "predicted":result.label,"raw_output":result.raw_output,"latency_ms":latency_ms}),
        );
    }
    let mut latencies = Vec::new();
    for _ in 0..warm_runs {
        let before = Instant::now();
        classifier.classify("Could you check this for me?")?;
        latencies.push(before.elapsed().as_secs_f64() * 1000.0);
    }
    latencies.sort_by(f64::total_cmp);
    let mean =
        (!latencies.is_empty()).then(|| latencies.iter().sum::<f64>() / latencies.len() as f64);
    let median = (!latencies.is_empty()).then(|| {
        let mid = latencies.len() / 2;
        if latencies.len() % 2 == 0 {
            (latencies[mid - 1] + latencies[mid]) / 2.0
        } else {
            latencies[mid]
        }
    });
    let p95 = (!latencies.is_empty())
        .then(|| latencies[(latencies.len() as f64 * 0.95).ceil() as usize - 1]);
    println!("{}", serde_json::to_string_pretty(&json!({
        "model":model,"tokenizer":tokenizer,"log_path":log,"cold_model_load_seconds":load_seconds,
        "examples":rows,"summary":{"example_count":rows.len(),"prediction_counts":counts,
        "labeled_count":CANONICAL.len(),"correct":correct,"accuracy":correct as f64/CANONICAL.len() as f64,
        "warm_runs":warm_runs,"warm_mean_ms":mean,"warm_median_ms":median,"warm_p95_ms":p95,
        "throughput_messages_per_second":mean.map(|mean|1000.0/mean)}})).unwrap());
    Ok(())
}
fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            std::process::ExitCode::FAILURE
        }
    }
}
