//! Firmware-rendered LED samples are buffered for display-synced playback in
//! the WebView. No device output or animation parsing happens in JavaScript.
use serde::Serialize;
use serde_json::Value;
use sidepulse_device::{animations::program_for_style, led_runtime::LedRuntime};
use std::collections::BTreeMap;

pub const SAMPLE_RATE: u64 = 120;
pub const BATCH_SIZE: usize = 240;

#[derive(Serialize)]
pub struct FrameBatch {
    pub program: Option<String>,
    pub start_index: u64,
    pub sample_rate: u64,
    pub frames: Vec<[u32; 8]>,
    pub error: Option<String>,
}

struct Cached {
    source: String,
    next_index: u64,
    runtime: Result<LedRuntime, String>,
}

#[derive(Default)]
pub struct Previews(BTreeMap<String, Cached>);

impl Previews {
    fn batch(&mut self, key: &str, source: Result<String, String>) -> FrameBatch {
        let source = match source {
            Ok(source) => source,
            Err(error) => {
                self.0.remove(key);
                return FrameBatch {
                    program: None,
                    start_index: 0,
                    sample_rate: SAMPLE_RATE,
                    frames: vec![],
                    error: Some(error),
                };
            }
        };
        if self.0.get(key).is_none_or(|cache| cache.source != source) {
            let runtime = (|| {
                let mut runtime = LedRuntime::new(8).map_err(|error| error.to_string())?;
                runtime
                    .parse(
                        &sidepulse_device::virtual_led::compact_preview_program(&source),
                        0,
                    )
                    .map_err(|error| error.to_string())?;
                Ok(runtime)
            })();
            self.0.insert(
                key.into(),
                Cached {
                    source,
                    next_index: 0,
                    runtime,
                },
            );
        }
        let cache = self.0.get_mut(key).unwrap();
        let start_index = cache.next_index;
        let mut frames = Vec::with_capacity(BATCH_SIZE);
        let result = cache
            .runtime
            .as_mut()
            .map_err(|error| error.clone())
            .and_then(|runtime| {
                for offset in 0..BATCH_SIZE as u64 {
                    let index = start_index + offset;
                    let millis = (index * 1000 / SAMPLE_RATE) as u32;
                    let colors = runtime.step(millis).map_err(|error| error.to_string())?;
                    let mut packed = [0; 8];
                    for (pixel, [red, green, blue]) in packed.iter_mut().zip(colors) {
                        *pixel = (u32::from(red) << 16) | (u32::from(green) << 8) | u32::from(blue);
                    }
                    frames.push(packed);
                }
                Ok(())
            });
        match result {
            Ok(()) => {
                cache.next_index += BATCH_SIZE as u64;
                FrameBatch {
                    program: Some(cache.source.clone()),
                    start_index,
                    sample_rate: SAMPLE_RATE,
                    frames,
                    error: None,
                }
            }
            Err(error) => FrameBatch {
                program: Some(cache.source.clone()),
                start_index,
                sample_rate: SAMPLE_RATE,
                frames: vec![],
                error: Some(error),
            },
        }
    }

    pub fn batches(&mut self, state: &Value, editor: Option<&str>) -> BTreeMap<String, FrameBatch> {
        let mut result = BTreeMap::new();
        for key in sidepulse_core::ANIMATION_STATES {
            if matches!(key, "tool_running" | "long_task_progress") {
                continue;
            }
            result.insert(key.into(), self.batch(key, source(state, key)));
        }
        if let Some(program) = editor {
            result.insert("editor".into(), self.batch("editor", Ok(program.into())));
        }
        result
    }
}

pub fn source(state: &Value, key: &str) -> Result<String, String> {
    let library = &state["animation_library"];
    let style = library["current"][key].as_str().unwrap_or("default");
    let custom = if style.starts_with("custom:") {
        library["custom_animations"][style]["program"]
            .as_str()
            .ok_or("Custom animation is unavailable.")?
    } else {
        state["animation_states"]
            .as_array()
            .and_then(|states| {
                states
                    .iter()
                    .find(|item| item["mode"].as_str() == Some(key))
            })
            .and_then(|item| item["program"].as_str())
            .unwrap_or("")
    };
    let mode = serde_json::from_value(serde_json::json!(key))
        .unwrap_or(sidepulse_core::AgentMode::IdleReady);
    program_for_style(
        mode,
        8,
        255,
        if style == "default" {
            sidepulse_core::default_animation(key)
        } else if style.starts_with("custom:") {
            "custom"
        } else {
            style
        },
        custom,
    )
    .map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn batches_continue_without_restarting_and_editor_changes_reset_the_timeline() {
        let state: Value = serde_json::from_str(include_str!("../tests/state.json")).unwrap();
        let mut renderer = Previews::default();
        let first = renderer.batches(&state, Some("#FF0080"));
        assert_eq!(first.len(), 9);
        assert!(first.values().all(|batch| {
            batch.error.is_none() && batch.frames.len() == BATCH_SIZE && batch.start_index == 0
        }));
        assert_eq!(first["editor"].frames[0], [0xFF0080; 8]);
        let next = renderer.batches(&state, Some("#FF0080"));
        assert_eq!(next["editor"].start_index, BATCH_SIZE as u64);
        let changed = renderer.batches(&state, Some("#00FF00"));
        assert_eq!(changed["editor"].start_index, 0);
        assert_eq!(changed["editor"].frames[0], [0x00FF00; 8]);
        let invalid = renderer.batches(&state, Some("not a LED program"));
        assert!(invalid["editor"].error.is_some());
        assert!(invalid["working"].error.is_none());
    }
}
