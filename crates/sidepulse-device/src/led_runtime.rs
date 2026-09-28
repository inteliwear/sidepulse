//! Rust host for the bundled firmware LED engine. No GUI or JavaScript runtime.

use std::io;
use std::sync::OnceLock;

use wasmi::{Config, Engine, Linker, Memory, Module, Store, TypedFunc};

use crate::validate_led_text;

const FUEL_PER_CALL: u64 = 2_000_000;
const ERROR_NAMES: [&str; 12] = [
    "ok",
    "null-input",
    "too-long",
    "too-many-lines",
    "too-many-animation-lines",
    "syntax",
    "bad-color",
    "bad-index",
    "bad-time",
    "bad-brightness",
    "bad-repeat",
    "trailing-input",
];

fn runtime_module() -> io::Result<&'static (Engine, Module)> {
    static MODULE: OnceLock<Result<(Engine, Module), String>> = OnceLock::new();
    MODULE
        .get_or_init(|| {
            let mut config = Config::default();
            config.consume_fuel(true);
            let engine = Engine::new(&config);
            let bytes = include_bytes!("../../../src/sidepulse/resources/sdled.wasm");
            let module =
                Module::new(&engine, bytes.as_slice()).map_err(|error| error.to_string())?;
            Ok((engine, module))
        })
        .as_ref()
        .map_err(|error| io::Error::other(error.clone()))
}

pub struct LedRuntime {
    store: Store<()>,
    memory: Memory,
    input: usize,
    output: usize,
    count: usize,
    parse: TypedFunc<(i32, i32, i32), i32>,
    step: TypedFunc<(i32, i32), ()>,
}

impl LedRuntime {
    pub fn new(led_count: usize) -> io::Result<Self> {
        let (engine, module) = runtime_module()?;
        let mut store = Store::new(engine, ());
        store.set_fuel(FUEL_PER_CALL).map_err(runtime_error)?;
        let instance = Linker::new(engine)
            .instantiate_and_start(&mut store, module)
            .map_err(runtime_error)?;
        let memory = instance
            .get_memory(&store, "memory")
            .ok_or_else(|| io::Error::other("LED engine has no memory"))?;
        let input = instance
            .get_typed_func::<(), i32>(&store, "sdled_input_ptr")
            .map_err(runtime_error)?
            .call(&mut store, ())
            .map_err(runtime_error)? as usize;
        let output = instance
            .get_typed_func::<(), i32>(&store, "sdled_output_ptr")
            .map_err(runtime_error)?
            .call(&mut store, ())
            .map_err(runtime_error)? as usize;
        let count = if led_count == 2 { 2 } else { 8 };
        instance
            .get_typed_func::<(i32, i32), ()>(&store, "sdled_reset")
            .map_err(runtime_error)?
            .call(&mut store, (count as i32, 0))
            .map_err(runtime_error)?;
        let parse = instance
            .get_typed_func(&store, "sdled_parse")
            .map_err(runtime_error)?;
        let step = instance
            .get_typed_func(&store, "sdled_step")
            .map_err(runtime_error)?;
        Ok(Self {
            store,
            memory,
            input,
            output,
            count,
            parse,
            step,
        })
    }

    pub fn parse(&mut self, program: &str, now_ms: u32) -> io::Result<()> {
        validate_led_text(program)?;
        self.store.set_fuel(FUEL_PER_CALL).map_err(runtime_error)?;
        self.memory
            .write(&mut self.store, self.input, &[0; 512])
            .map_err(runtime_error)?;
        self.memory
            .write(&mut self.store, self.input, program.as_bytes())
            .map_err(runtime_error)?;
        let packed = self
            .parse
            .call(
                &mut self.store,
                (self.count as i32, program.len() as i32, now_ms as i32),
            )
            .map_err(runtime_error)? as u32;
        if packed & 1 == 0 {
            let error = (packed >> 8) & 255;
            let name = ERROR_NAMES
                .get(error as usize)
                .copied()
                .unwrap_or("unknown");
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!(
                    "LED program {name} at line {}, column {}",
                    (packed >> 16) & 255,
                    (packed >> 24) & 255
                ),
            ));
        }
        Ok(())
    }

    pub fn step(&mut self, now_ms: u32) -> io::Result<Vec<[u8; 3]>> {
        self.store.set_fuel(FUEL_PER_CALL).map_err(runtime_error)?;
        self.step
            .call(&mut self.store, (self.count as i32, now_ms as i32))
            .map_err(runtime_error)?;
        let mut bytes = [0; 24];
        self.memory
            .read(&self.store, self.output, &mut bytes[..self.count * 3])
            .map_err(runtime_error)?;
        Ok(bytes[..self.count * 3]
            .chunks_exact(3)
            .map(|rgb| [rgb[0], rgb[1], rgb[2]])
            .collect())
    }
}

pub fn validate_program(program: &str, count: usize) -> io::Result<()> {
    LedRuntime::new(count)?.parse(program, 0)
}

fn runtime_error(error: impl std::fmt::Display) -> io::Error {
    io::Error::other(error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::animations::{BUILTIN_ANIMATIONS, program_for_style};
    use sidepulse_core::AgentMode;

    #[test]
    fn firmware_accepts_all_bundled_programs_for_both_device_sizes() {
        for count in [2, 8] {
            let mut runtime = LedRuntime::new(count).unwrap();
            for (id, _) in BUILTIN_ANIMATIONS {
                let program = program_for_style(AgentMode::Working, count, 255, id, "").unwrap();
                runtime
                    .parse(&program, 0)
                    .unwrap_or_else(|error| panic!("{id}/{count}: {error}"));
                assert_eq!(runtime.step(100).unwrap().len(), count);
            }
        }
    }

    #[test]
    fn firmware_reports_syntax_errors_and_preserves_rgb_and_brightness() {
        let mut runtime = LedRuntime::new(8).unwrap();
        assert!(
            runtime
                .parse("not an animation", 0)
                .unwrap_err()
                .to_string()
                .contains("syntax")
        );
        runtime.parse("#FF0080", 0).unwrap();
        assert_eq!(runtime.step(1).unwrap(), vec![[255, 0, 128]; 8]);
        runtime.parse("brightness 0\n#FF0080", 2).unwrap();
        assert_eq!(runtime.step(3).unwrap(), vec![[0, 0, 0]; 8]);
    }
}
