use std::{
    io::{self, Read},
    path::PathBuf,
    process::ExitCode,
};
fn main() -> ExitCode {
    let mut model = None;
    let mut tokenizer = None;
    let mut cache = None;
    let mut download = false;
    let mut json = false;
    let mut messages = Vec::new();
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--help" | "-h" => {
                println!(
                    "Usage: sidepulse-next-reply [MESSAGE...] [--json] [--cache-dir PATH]\n       sidepulse-next-reply --download-model [--cache-dir PATH]\n       sidepulse-next-reply --model MODEL.gguf --tokenizer tokenizer.json [MESSAGE...]\n\nReads stdin when MESSAGE is omitted. Inference is local. Download the default model explicitly before first use."
                );
                return ExitCode::SUCCESS;
            }
            "--json" => json = true,
            "--download-model" => download = true,
            "--model" | "--tokenizer" | "--cache-dir" => {
                let Some(value) = args.next() else {
                    eprintln!("{arg} requires a value");
                    return ExitCode::from(2);
                };
                match arg.as_str() {
                    "--model" => model = Some(value),
                    "--tokenizer" => tokenizer = Some(PathBuf::from(value)),
                    _ => cache = Some(PathBuf::from(value)),
                }
            }
            _ if arg.starts_with('-') => {
                eprintln!("unexpected option {arg}");
                return ExitCode::from(2);
            }
            _ => messages.push(arg),
        }
    }
    let result = (|| -> io::Result<()> {
        let cache = cache.map_or_else(sidepulse_reply::model_files::default_cache, Ok)?;
        let (model, tokenizer) = if let Some(model) = model.filter(|model| {
            ![
                sidepulse_reply::model_files::MODEL_ID,
                sidepulse_reply::model_files::LEGACY_MODEL_ID,
            ]
            .contains(&model.as_str())
        }) {
            if download {
                return Err(io::Error::other(
                    "--download-model downloads only the default model",
                ));
            }
            (
                PathBuf::from(model),
                tokenizer.ok_or_else(|| {
                    io::Error::other("a custom GGUF model requires --tokenizer FILE")
                })?,
            )
        } else {
            if tokenizer.is_some() {
                return Err(io::Error::other("use --model PATH with a custom tokenizer"));
            }
            if download {
                sidepulse_reply::model_files::download_default(&cache)?
            } else {
                sidepulse_reply::model_files::cached_default(&cache)?
            }
        };
        if download && messages.is_empty() {
            println!(
                "Local reply model downloaded to {}",
                model.parent().unwrap().display()
            );
            return Ok(());
        }
        let message = if messages.is_empty() {
            let mut message = String::new();
            io::stdin().take(65537).read_to_string(&mut message)?;
            message
        } else {
            messages.join(" ")
        };
        let result =
            sidepulse_reply::ReplyClassifier::load(&model, &tokenizer)?.classify(&message)?;
        if json {
            println!("{}", serde_json::to_string(&result).unwrap());
        } else {
            println!("{}", result.label.as_str());
        }
        Ok(())
    })();
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("sidepulse-next-reply: {error}");
            ExitCode::FAILURE
        }
    }
}
