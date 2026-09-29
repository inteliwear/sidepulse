//! Temporary device previews never change saved animation or display settings.
use super::*;
pub(super) struct Preview {
    pub target: PathBuf,
    pub program: String,
    pub until: Instant,
}
impl Service {
    pub fn preview_animation(
        &self,
        state: &str,
        program: Option<&str>,
        seconds: u8,
    ) -> io::Result<()> {
        self.preview_animation_at(state, program, seconds, Instant::now())
    }
    fn preview_animation_at(
        &self,
        state: &str,
        program: Option<&str>,
        seconds: u8,
        now: Instant,
    ) -> io::Result<()> {
        ClientRequest {
            version: PROTOCOL_VERSION,
            request_id: 1,
            kind: RequestKind::PreviewAnimation {
                state: state.into(),
                program: program.map(str::to_owned),
                seconds,
            },
        }
        .validate()
        .map_err(io::Error::other)?;
        let mut device = self.device.lock().map_err(poisoned)?;
        let output = device.as_mut().ok_or_else(|| {
            io::Error::other("Connect an agent-display device before showing an animation.")
        })?;
        let settings = self.settings.lock().map_err(poisoned)?;
        let store = settings.as_ref();
        if store.is_some_and(|store| store.display_for_device(output.target()) != "agent") {
            return Err(io::Error::other(
                "Choose Agent Status on the device before showing an animation.",
            ));
        }
        let count = led_count_for_target(output.target());
        let program = if let Some(program) = program {
            sidepulse_device::apply_brightness(program, output.brightness())
        } else {
            let (style, custom) = store.map_or_else(
                || {
                    Ok((
                        sidepulse_core::default_animation(state).into(),
                        String::new(),
                    ))
                },
                |store| store.animation_for_state(state),
            )?;
            let mode = serde_json::from_value(serde_json::json!(state))
                .unwrap_or(sidepulse_core::AgentMode::IdleReady);
            program_for_style(mode, count, output.brightness(), &style, &custom)?
        };
        sidepulse_device::led_runtime::validate_program(&program, count)?;
        output.sync_program(&program)?;
        *self.animation_preview.lock().map_err(poisoned)? = Some(Preview {
            target: output.target().into(),
            program,
            until: now + Duration::from_secs(u64::from(seconds)),
        });
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn preview_restores_current_agent_output_without_saving_preferences() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("LEDS.LED");
        let settings = root.path().join("settings.json");
        let service = Service::default();
        fs::write(&settings, "{}").unwrap();
        service.configure_settings(&settings).unwrap();
        *service.device.lock().unwrap() = Some(DeviceOutput::with_target(&target, 255));
        let before = fs::read(&settings).unwrap();
        let now = Instant::now();
        service
            .preview_animation_at("working", Some("#FF0080"), 3, now)
            .unwrap();
        assert!(fs::read_to_string(&target).unwrap().contains("#FF0080"));
        service
            .sync_device_with_battery_at(None, now + Duration::from_secs(2))
            .unwrap();
        assert!(fs::read_to_string(&target).unwrap().contains("#FF0080"));
        service
            .sync_device_with_battery_at(None, now + Duration::from_secs(4))
            .unwrap();
        assert!(!fs::read_to_string(&target).unwrap().contains("#FF0080"));
        assert_eq!(fs::read(&settings).unwrap(), before);
        for (state, program, seconds) in [
            ("bad", Some("#FF0080"), 3),
            ("working", Some("invalid"), 3),
            ("working", Some("#FF0080"), 255),
        ] {
            assert!(service.preview_animation(state, program, seconds).is_err());
        }
        service
            .preview_animation("working", Some("#00FF00"), 3)
            .unwrap();
        service.set_display_mode("custom").unwrap();
        assert!(service.animation_preview.lock().unwrap().is_none());
        assert!(service.preview_animation("working", None, 3).is_err());
        assert!(
            Service::default()
                .preview_animation("working", None, 3)
                .is_err()
        );
    }
}
