//! Presentation frames use the same Rust/firmware renderer as native LED output.
use serde::Serialize;
use serde_json::Value;
use sidepulse_device::{animations::program_for_style, led_runtime::LedRuntime};
use std::{collections::BTreeMap, time::Instant};

#[derive(Serialize)]
pub struct Frame {
    program: Option<String>,
    colors: Vec<[u8; 3]>,
    error: Option<String>,
}
struct Cached {
    source: String,
    started: Instant,
    runtime: Result<LedRuntime, String>,
}
#[derive(Default)]
pub struct Previews(BTreeMap<String, Cached>);
impl Previews {
    fn frame(&mut self, key: &str, source: Result<String, String>) -> Frame {
        let source = match source {
            Ok(source) => source,
            Err(error) => {
                return Frame {
                    program: None,
                    colors: vec![],
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
                    started: Instant::now(),
                    runtime,
                },
            );
        }
        let cache = self.0.get_mut(key).unwrap();
        let colors = cache
            .runtime
            .as_mut()
            .map_err(|error| error.clone())
            .and_then(|runtime| {
                runtime
                    .step(cache.started.elapsed().as_millis() as u32)
                    .map_err(|error| error.to_string())
            });
        match colors {
            Ok(colors) => Frame {
                program: Some(cache.source.clone()),
                colors,
                error: None,
            },
            Err(error) => Frame {
                program: Some(cache.source.clone()),
                colors: vec![],
                error: Some(error),
            },
        }
    }
    pub fn frames(&mut self, state: &Value, editor: Option<&str>) -> BTreeMap<String, Frame> {
        let mut result = BTreeMap::new();
        for key in sidepulse_core::ANIMATION_STATES {
            if matches!(key, "tool_running" | "long_task_progress") {
                continue;
            }
            result.insert(key.into(), self.frame(key, source(state, key)));
        }
        if let Some(program) = editor {
            result.insert("editor".into(), self.frame("editor", Ok(program.into())));
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
    fn saved_rows_and_editor_use_firmware_frames_without_device_access() {
        let state: Value = serde_json::from_str(include_str!("../tests/state.json")).unwrap();
        let mut renderer = Previews::default();
        let frames = renderer.frames(&state, Some("#FF0080"));
        assert_eq!(frames.len(), 9);
        assert!(
            frames
                .values()
                .all(|frame| frame.error.is_none() && frame.colors.len() == 8)
        );
        assert_eq!(frames["editor"].colors, vec![[255, 0, 128]; 8]);
        let frames = renderer.frames(&state, Some("not a LED program"));
        assert!(frames["editor"].error.is_some());
        assert!(frames["working"].error.is_none());
    }
}
