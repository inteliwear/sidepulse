use super::{Classification, build_prompt, parse_label};
use candle_core::{Device, Tensor, quantized::gguf_file};
use candle_transformers::models::quantized_qwen2::ModelWeights;
use std::{
    collections::HashSet,
    fs::File,
    io,
    path::{Path, PathBuf},
};
use tokenizers::Tokenizer;
fn error(error: impl std::fmt::Display) -> io::Error {
    io::Error::other(error.to_string())
}
fn load_model(path: &Path) -> io::Result<ModelWeights> {
    let mut file = File::open(path)?;
    if file.metadata()?.len() > 2 * 1024 * 1024 * 1024 {
        return Err(error("model file is too large"));
    }
    let content = gguf_file::Content::read(&mut file).map_err(error)?;
    if content
        .metadata
        .get("general.architecture")
        .and_then(|value| value.to_string().ok())
        .map(String::as_str)
        != Some("qwen2")
    {
        return Err(error(
            "the local classifier requires a Qwen2 / Qwen2.5 GGUF model",
        ));
    }
    ModelWeights::from_gguf(content, &mut file, &Device::Cpu).map_err(error)
}
pub struct ReplyClassifier {
    model: ModelWeights,
    tokenizer: Tokenizer,
    model_path: PathBuf,
    shapes: HashSet<usize>,
    stop_ids: Vec<u32>,
}
impl ReplyClassifier {
    pub fn load(model: &Path, tokenizer: &Path) -> io::Result<Self> {
        let tokenizer = Tokenizer::from_file(tokenizer).map_err(error)?;
        let stop_ids = ["<|im_end|>", "<|endoftext|>"]
            .into_iter()
            .filter_map(|token| tokenizer.token_to_id(token))
            .collect();
        Ok(Self {
            model: load_model(model)?,
            tokenizer,
            model_path: model.into(),
            shapes: HashSet::new(),
            stop_ids,
        })
    }
    pub fn classify(&mut self, message: &str) -> io::Result<Classification> {
        if message.trim().is_empty() {
            return Err(error("message must not be empty"));
        }
        if message.len() > 65536 {
            return Err(error("message is too large"));
        }
        let tokens = self
            .tokenizer
            .encode(build_prompt(message), false)
            .map_err(error)?;
        let tokens = tokens.get_ids();
        if tokens.len() > 4096 {
            return Err(error(
                "message exceeds the local classifier's 4096-token limit",
            ));
        }
        // Candle also caches attention masks by prompt length. Bound those
        // cached shapes across long benchmark runs and repeated conversations.
        if !self.shapes.contains(&tokens.len()) && self.shapes.len() >= 32 {
            self.model = load_model(&self.model_path)?;
            self.shapes.clear();
        }
        self.shapes.insert(tokens.len());
        // This backend resets every layer's KV cache at index position zero.
        let mut generated = Vec::new();
        let mut logits = self
            .model
            .forward(
                &Tensor::new(tokens, &Device::Cpu)
                    .map_err(error)?
                    .unsqueeze(0)
                    .map_err(error)?,
                0,
            )
            .map_err(error)?
            .squeeze(0)
            .map_err(error)?;
        for step in 0..2 {
            let token = logits
                .argmax(candle_core::D::Minus1)
                .map_err(error)?
                .to_scalar::<u32>()
                .map_err(error)?;
            if self.stop_ids.contains(&token) {
                break;
            }
            generated.push(token);
            if step == 0 {
                logits = self
                    .model
                    .forward(
                        &Tensor::new(&[token], &Device::Cpu)
                            .map_err(error)?
                            .unsqueeze(0)
                            .map_err(error)?,
                        tokens.len(),
                    )
                    .map_err(error)?
                    .squeeze(0)
                    .map_err(error)?;
            }
        }
        let raw_output = self
            .tokenizer
            .decode(&generated, true)
            .map_err(error)?
            .trim()
            .to_owned();
        Ok(Classification {
            label: parse_label(&raw_output),
            raw_output,
        })
    }
}
