# Native Rust examples

The original examples now have Rust implementations. Their monitoring and LED
policies are separate from command-line presentation. Original Python files are
retained as parity references until cutover.

## Audio meter

Offline rendering opens no microphone and writes no device:

```sh
cargo run -p sidepulse-examples --example audio_monitor -- --level 0.5 --dry-run
```

Live input uses [CPAL](https://github.com/RustAudio/cpal), with native CoreAudio,
WASAPI, and ALSA backends:

```sh
cargo run -p sidepulse-examples --features live-audio --example audio_monitor -- --list-inputs
cargo run -p sidepulse-examples --features live-audio --example audio_monitor -- --dry-run --terminal
```

`--dry-run` disables device writes; the live mode still reads the selected audio
input. Use `--level` for a preview without capture. No audio file is stored.
Live capture needs macOS 14.2+ with microphone permission, Windows 10+, or ALSA
on Linux. Linux builds require `libasound2-dev` or the distribution equivalent.
The application's background service does not depend on the audio feature.

The meter retains the original RMS/dBFS mapping, gain and curve, attack/release
smoothing, eight-sample queue, green-to-red bar, dim baseline, global brightness,
transition duration, configurable input/rate/block size, deduplicated writes,
and explicit `--off-on-exit`. `--help` lists all controls.

## Score display

The fixed Spain/Argentina demonstration and original scoreboard URL are retained
from the source example; these defaults do not assert the teams in a real final.
Use a fixed score or captured scoreboard for offline evaluation:

```sh
cargo run -p sidepulse-examples --example fifa_final -- --score 2-1 --once --dry-run
cargo run -p sidepulse-examples --example fifa_final -- --scoreboard-file captured.json --once --dry-run
```

Without these overrides the example fetches the configured scoreboard with a
15-second timeout and a 1 MiB response bound. It preserves goal saturation,
left/right team colors, dim baseline, transitions, score-change celebration,
full-time holding, polling, Ctrl-C, and explicit LED shutdown on exit.

## Verification

57 captured Python audio/score LED programs match exactly, including fractional
levels, brightness commands, irregular LED counts, and saturated scores.
Tests cover level mapping, smoothing, incomplete/unrelated scoreboard events,
mock-device dry runs, and explicit shutdown. CI compiles the live audio adapter
on macOS, Windows, and Linux. Implementation verification has not captured live
microphone samples or fetched the external scoreboard.
