//! Temporary device previews never change saved animation or display settings.
use super::*;
pub(super) struct Preview {
    pub programs: Vec<(PathBuf, String)>,
    pub previous_programs: Vec<(PathBuf, String)>,
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
        let mut extras = self.extra_devices.lock().map_err(poisoned)?;
        let settings = self.settings.lock().map_err(poisoned)?;
        let store = settings.as_ref();
        let mut programs = Vec::new();
        for output in device.as_ref().into_iter().chain(extras.iter()) {
            if store.is_some_and(|store| store.display_for_device(output.target()) != "agent") {
                continue;
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
            programs.push((output.target().to_path_buf(), program));
        }
        if programs.is_empty() {
            return Err(io::Error::other(
                "Connect an agent-display device before showing an animation.",
            ));
        }
        let mut active_preview = self.animation_preview.lock().map_err(poisoned)?;
        let previous_programs = programs
            .iter()
            .filter_map(|(target, _)| {
                active_preview
                    .as_ref()
                    .and_then(|active| {
                        active
                            .previous_programs
                            .iter()
                            .find(|(previous_target, _)| previous_target == target)
                            .map(|(_, previous)| previous.clone())
                    })
                    .or_else(|| {
                        fs::read_to_string(target)
                            .ok()
                            .filter(|program| sidepulse_device::validate_led_text(program).is_ok())
                    })
                    .map(|program| (target.clone(), program))
            })
            .collect();
        for output in device.as_mut().into_iter().chain(extras.iter_mut()) {
            if let Some((_, program)) = programs
                .iter()
                .find(|(target, _)| target == output.target())
            {
                output.sync_program(program)?;
            }
        }
        *active_preview = Some(Preview {
            programs,
            previous_programs,
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

    #[test]
    fn preview_shows_on_every_agent_device_with_its_own_brightness() {
        let root = tempfile::tempdir().unwrap();
        let dot = root.path().join("PulseDot");
        let pro = root.path().join("SidePulsePro");
        let manual = root.path().join("ManualDevice");
        for path in [&dot, &pro, &manual] {
            fs::create_dir(path).unwrap();
        }
        let settings = root.path().join("settings.json");
        fs::write(
            &settings,
            serde_json::json!({"devices": [
                {"path": dot, "led_display": "agent"},
                {"path": pro, "led_display": "agent"},
                {"path": manual, "led_display": "custom"}
            ]})
            .to_string(),
        )
        .unwrap();
        let service = Service::default();
        service.configure_settings(&settings).unwrap();
        *service.device.lock().unwrap() = Some(DeviceOutput::new(&dot, 128));
        *service.extra_devices.lock().unwrap() = vec![
            DeviceOutput::new(&pro, 255),
            DeviceOutput::new(&manual, 255),
        ];
        service.preview_animation("working", None, 3).unwrap();
        let dot_program = fs::read_to_string(dot.join("LEDS.LED")).unwrap();
        let pro_program = fs::read_to_string(pro.join("LEDS.LED")).unwrap();
        assert_ne!(dot_program, pro_program);
        assert!(!manual.join("LEDS.LED").exists());
        assert_eq!(
            service
                .animation_preview
                .lock()
                .unwrap()
                .as_ref()
                .unwrap()
                .programs
                .len(),
            2
        );
        service
            .sync_device_with_battery_at(None, Instant::now() + Duration::from_secs(4))
            .unwrap();
        assert_ne!(
            fs::read_to_string(dot.join("LEDS.LED")).unwrap(),
            dot_program
        );
        assert_ne!(
            fs::read_to_string(pro.join("LEDS.LED")).unwrap(),
            pro_program
        );
    }

    #[test]
    fn preview_restores_the_previous_program_while_lid_output_is_held() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("LEDS.LED");
        let settings = root.path().join("settings.json");
        fs::write(&settings, r#"{"sleep_prevention_policy":"never"}"#).unwrap();
        let service = Service::new();
        service.configure_settings(&settings).unwrap();
        service.configure_device(&target, 255).unwrap();
        service
            .set_animation_state("lid_closed", "custom", Some("#222222"))
            .unwrap();
        let now = Instant::now();
        service
            .observe_power(&sidepulse_core::PowerSnapshot {
                lid_closed: Some(false),
                ..Default::default()
            })
            .unwrap();
        service.render_device_with_battery_at(None, now).unwrap();
        service
            .observe_power(&sidepulse_core::PowerSnapshot {
                lid_closed: Some(true),
                ..Default::default()
            })
            .unwrap();
        service
            .render_device_with_battery_at(None, now + Duration::from_millis(10))
            .unwrap();
        service
            .render_device_with_battery_at(None, now + Duration::from_secs(2))
            .unwrap();
        assert_eq!(fs::read_to_string(&target).unwrap(), "#222222");
        service
            .preview_animation_at("working", Some("#FF0080"), 3, now + Duration::from_secs(3))
            .unwrap();
        assert_eq!(fs::read_to_string(&target).unwrap(), "#FF0080");
        service
            .preview_animation_at(
                "completed",
                Some("#00FF00"),
                3,
                now + Duration::from_secs(4),
            )
            .unwrap();
        assert_eq!(fs::read_to_string(&target).unwrap(), "#00FF00");
        service
            .render_device_with_battery_at(None, now + Duration::from_secs(8))
            .unwrap();
        assert_eq!(fs::read_to_string(&target).unwrap(), "#222222");
    }
}
