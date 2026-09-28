//! Service-owned LED engine state; UI clients receive pixels only.

use std::io;
use std::time::Instant;

use sidepulse_device::led_runtime::LedRuntime;

pub struct VirtualOutput {
    started: Instant,
    program: String,
    runtime: Option<LedRuntime>,
}

impl Default for VirtualOutput {
    fn default() -> Self {
        Self {
            started: Instant::now(),
            program: String::new(),
            runtime: None,
        }
    }
}

impl VirtualOutput {
    pub fn pixels(&mut self, program: &str) -> io::Result<Vec<[u8; 3]>> {
        let now_ms = self.started.elapsed().as_millis() as u32;
        if self.runtime.is_none() {
            self.runtime = Some(LedRuntime::new(8)?);
        }
        let runtime = self.runtime.as_mut().unwrap();
        if self.program != program {
            runtime.parse(program, now_ms)?;
            self.program = program.into();
        }
        runtime.step(now_ms)
    }

    pub fn clear(&mut self) {
        self.program.clear();
        self.runtime = None;
    }
}
