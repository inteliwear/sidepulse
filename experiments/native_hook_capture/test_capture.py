from __future__ import annotations

import concurrent.futures
import os
import socket
import struct
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BINARY = ROOT / "capture"
SOURCE = ROOT / "capture.cpp"
FIXED_HEADER = struct.Struct(">4sIQQIIIII")


@dataclass(frozen=True)
class Frame:
    realtime_ns: int
    monotonic_ns: int
    provider: bytes
    event: bytes
    log_path: bytes
    context: bytes
    payload: bytes


def build() -> None:
    subprocess.run(
        [
            "clang++",
            "-std=c++20",
            "-O2",
            "-DNDEBUG",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(SOURCE),
            "-o",
            str(BINARY),
        ],
        check=True,
    )


def decode(path: Path) -> list[Frame]:
    data = path.read_bytes()
    frames: list[Frame] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < FIXED_HEADER.size:
            raise AssertionError("incomplete frame header")
        (
            magic,
            body_length,
            realtime_ns,
            monotonic_ns,
            provider_length,
            event_length,
            log_length,
            context_length,
            payload_length,
        ) = FIXED_HEADER.unpack_from(data, offset)
        if magic != b"SPH1":
            raise AssertionError(f"unexpected magic at {offset}: {magic!r}")
        frame_end = offset + 8 + body_length
        if frame_end > len(data):
            raise AssertionError("incomplete frame body")
        cursor = offset + FIXED_HEADER.size

        def take(length: int) -> bytes:
            nonlocal cursor
            result = data[cursor : cursor + length]
            cursor += length
            return result

        provider = take(provider_length)
        event = take(event_length)
        log_path = take(log_length)
        context = take(context_length)
        payload = take(payload_length)
        if cursor != frame_end:
            raise AssertionError("frame lengths do not match body length")
        frames.append(
            Frame(realtime_ns, monotonic_ns, provider, event, log_path, context, payload)
        )
        offset = frame_end
    return frames


def decode_context(data: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise AssertionError("incomplete context header")
        key_length, value_length = struct.unpack_from(">II", data, offset)
        offset += 8
        end = offset + key_length + value_length
        if end > len(data):
            raise AssertionError("incomplete context entry")
        key = data[offset : offset + key_length].decode()
        offset += key_length
        value = data[offset : offset + value_length].decode(errors="surrogateescape")
        offset += value_length
        result[key] = value
    return result


def capture(
    spool: Path,
    payload: bytes,
    *,
    provider: str = "claude",
    event: str | None = None,
    notify_socket: Path | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = [
        str(BINARY),
        "--provider",
        provider,
        "--log",
        f"/tmp/{provider}.jsonl",
        "--spool",
        str(spool),
    ]
    if event is not None:
        command.extend(["--event", event])
    if notify_socket is not None:
        command.extend(["--notify-socket", str(notify_socket)])
    return subprocess.run(
        command,
        input=payload,
        capture_output=True,
        check=False,
        env=environment,
    )


class CaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        build()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sidepulse-capture-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.spool = self.root / "events.spool"

    def test_raw_payloads_round_trip_without_json_processing(self) -> None:
        payloads = (
            b"",
            b"   \n\t ",
            b"this is not json",
            b'{"hook_event_name":"PreToo',
            b"null",
            b"[1,2,3]",
            '{"message":"emoji 🚀 ünïcode"}'.encode(),
            b'{"message":"a\\u0000b","path":"C:\\\\tmp"}',
            b'{"message":"' + (b"x" * 500_000) + b'"}',
            b"literal\x00bytes",
        )
        for payload in payloads:
            result = capture(self.spool, payload)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertEqual(result.stderr, b"")
        self.assertEqual([frame.payload for frame in decode(self.spool)], list(payloads))

    def test_provider_event_and_log_metadata_round_trip(self) -> None:
        cases = (
            ("codex", None),
            ("claude", None),
            ("grok", None),
            ("cursor", "preToolUse"),
        )
        for provider, event in cases:
            capture(self.spool, b"{}", provider=provider, event=event)
        frames = decode(self.spool)
        self.assertEqual([frame.provider.decode() for frame in frames], [c[0] for c in cases])
        self.assertEqual([frame.event.decode() for frame in frames], [c[1] or "" for c in cases])
        self.assertEqual(
            [frame.log_path.decode() for frame in frames],
            [f"/tmp/{provider}.jsonl" for provider, _ in cases],
        )
        self.assertTrue(all(frame.realtime_ns > 0 for frame in frames))
        self.assertTrue(all(frame.monotonic_ns > 0 for frame in frames))

    def test_hook_environment_and_process_context_are_captured(self) -> None:
        environment = {
            **os.environ,
            "SIDEPULSE_AGENT_ORIGIN": "Claude in Test Host",
            "SIDEPULSE_AGENT_ORIGIN_KIND": "claude_test",
            "SIDEPULSE_DISABLE_EVENT_SOCKET": "1",
            "TERM_PROGRAM": "TestTerminal",
            "__CFBundleIdentifier": "io.sidepulse.tests",
            "VSCODE_IPC_HOOK_CLI": "/tmp/vscode.sock",
            "HOME": "/tmp/test-home",
            "XDG_STATE_HOME": "/tmp/test-state",
        }
        capture(self.spool, b"{}", environment=environment)
        context = decode_context(decode(self.spool)[0].context)
        self.assertEqual(context["env:SIDEPULSE_AGENT_ORIGIN"], "Claude in Test Host")
        self.assertEqual(context["env:SIDEPULSE_AGENT_ORIGIN_KIND"], "claude_test")
        self.assertEqual(context["env:SIDEPULSE_DISABLE_EVENT_SOCKET"], "1")
        self.assertEqual(context["env:TERM_PROGRAM"], "TestTerminal")
        self.assertEqual(context["env:__CFBundleIdentifier"], "io.sidepulse.tests")
        self.assertEqual(context["env:VSCODE_PRESENT"], "1")
        self.assertEqual(context["env:HOME"], "/tmp/test-home")
        self.assertEqual(context["env:XDG_STATE_HOME"], "/tmp/test-state")
        self.assertTrue(any(key.endswith(":pid") for key in context))
        self.assertTrue(any(key.endswith(":comm") for key in context))
        self.assertTrue(any(":arg:" in key for key in context))

    def test_sequential_invocations_preserve_order(self) -> None:
        expected = [f"event-{index}".encode() for index in range(100)]
        for payload in expected:
            capture(self.spool, payload)
        self.assertEqual([frame.payload for frame in decode(self.spool)], expected)

    def test_concurrent_writers_never_interleave_frames(self) -> None:
        expected = {f"writer-{index}".encode() for index in range(80)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda payload: capture(self.spool, payload), expected))
        self.assertTrue(all(result.returncode == 0 for result in results))
        self.assertEqual({frame.payload for frame in decode(self.spool)}, expected)

    def test_notification_is_nonblocking_and_contains_no_event_data(self) -> None:
        notify_path = self.root / "notify.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(listener.close)
        listener.bind(str(notify_path))
        listener.settimeout(1)
        payload = b'{"secret":"not sent over notification socket"}'
        result = capture(self.spool, payload, notify_socket=notify_path)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(listener.recv(16), b"\x01")
        self.assertEqual(decode(self.spool)[0].payload, payload)

    def test_missing_listener_and_bad_arguments_fail_open_silently(self) -> None:
        result = capture(self.spool, b"{}", notify_socket=self.root / "missing.sock")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        bad = subprocess.run([str(BINARY), "--provider"], capture_output=True)
        self.assertEqual((bad.returncode, bad.stdout, bad.stderr), (0, b"", b""))


if __name__ == "__main__":
    unittest.main()
