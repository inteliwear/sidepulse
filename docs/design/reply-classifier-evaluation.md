# Local reply classifier evaluation

The Rust preview uses Candle 0.9.2 on the CPU and the official
[Qwen2.5 0.5B GGUF](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF)
in Q4_K_M format. It preserves the existing prompt, two-token greedy generation,
label parser, positional message/stdin inputs, and JSON response shape. It runs
without Python or remote inference. The model download is explicit, pinned to a
revision, bounded, checksummed, and published atomically.

The original optional classifier uses MLX and a different four-bit conversion.
The new format is portable, but its predictions differ. It is not enabled in
the agent monitor. Numerical or behavioral parity is not claimed.

## Measured on September 27, 2026

Both implementations were run on the same eight canonical messages from the
maintained benchmark, with isolated caches and the exact same chat prompt. The
MLX model remained capable of answering a separate arithmetic question, and its
rendered prompt matched the Rust fixture exactly.

| Message | Expected | Python MLX | Rust GGUF |
| --- | --- | --- | --- |
| Can you send the document? | Reply | No reply | Reply |
| Please send the document. | Reply | No reply | Reply |
| Let me know when you arrive. | Reply | No reply | Reply |
| Which option should we choose? | Reply | No reply | No reply |
| Thanks, I received it. | No reply | No reply | No reply |
| Just an FYI, the deployment is complete. | No reply | No reply | No reply |
| Sounds good. | No reply | No reply | No reply |
| Have a good weekend. | No reply | No reply | No reply |

Rust scored 7/8; Python scored 4/8. Rust loaded the model in 0.56 seconds and
averaged 3.40 seconds per repeated input across three warm runs. The MLX run was
much faster after loading (roughly 0.11 seconds per input). These are a small
local smoke evaluation, not a general accuracy or performance guarantee.

The library bounds input size, token count, and cached prompt shapes. The Rust
benchmark loads once, evaluates the canonical set, optionally reads an explicitly
provided event log, and reports cold load and warm latency statistics:

```sh
sidepulse-next reply --download-model --cache-dir /tmp/sidepulse-reply-model
cargo run --release -p sidepulse-reply --example benchmark_reply_classifier -- \
  --cache-dir /tmp/sidepulse-reply-model --warm-runs 20
```

Further accuracy evaluation and optional GPU acceleration remain separate from
the monitor migration. The current classifier must not be used to suppress
permission requests or replace explicit provider lifecycle signals.
