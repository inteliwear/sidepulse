"""Agent hook stability tests.

SidePulse installs itself into the hook configuration of Claude Code, Codex,
and Grok. Those hooks run inside somebody else's agent session on every tool
call. The contract is therefore narrow and absolute:

1. **Never non-zero.** A failing hook can abort or degrade the agent turn.
   ``hook_log_main`` must return 0 for every input and every internal failure.
2. **Never noisy on stdout.** Hook stdout is interpreted by the agent runtime;
   stray output corrupts it.
3. **Never hang.** A blocking hook stalls every tool call.
4. **Still actually work.** Fail-open is only acceptable if the happy path
   genuinely logs -- otherwise "return 0" would pass these tests while
   silently recording nothing. ``HappyPathTests`` pins that down.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sidepulse import hook as hook_module
from sidepulse.cli import build_parser, build_sidepulse_parser, cmd_hook_log
from sidepulse.hook import format_hook_payload, hook_log_main, routed_hook_payload
from sidepulse.install import fail_open_command, hook_command

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

PROVIDERS = ("claude", "codex", "grok")

# Payloads a hook can plausibly be handed, including the ones nobody plans for.
HOSTILE_PAYLOADS = {
    "empty": "",
    "whitespace": "   \n\t ",
    "not_json": "this is not json at all",
    "truncated_json": '{"hook_event_name": "PreToo',
    "json_null": "null",
    "json_list": "[1, 2, 3]",
    "json_string": '"just a string"',
    "json_number": "42",
    "empty_object": "{}",
    "nested_deep": json.dumps({"a": {"b": {"c": {"d": {"e": "f"}}}}}),
    "unicode": json.dumps({"hook_event_name": "Stop", "message": "emoji 🚀 ünïcode"}),
    "null_bytes": '{"hook_event_name": "Stop", "message": "a\\u0000b"}',
    "huge": json.dumps({"hook_event_name": "Stop", "message": "x" * 500_000}),
    "wrong_types": json.dumps({"hook_event_name": 12345, "session_id": []}),
    "missing_event": json.dumps({"session_id": "abc"}),
}


class HookIsolationMixin:
    """Run hooks against a scratch HOME with the event socket disabled."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.log_path = self.home / "logs" / "hook.jsonl"

        env = patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "SIDEPULSE_DISABLE_EVENT_SOCKET": "1",
            },
        )
        env.start()
        self.addCleanup(env.stop)


def run_hook(provider: str, log_path: Path, payload: str) -> tuple[int, str]:
    """Drive hook_log_main with the given stdin, capturing stdout."""
    buffer = io.StringIO()
    with patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(buffer):
        code = hook_log_main(provider, log_path)
    return code, buffer.getvalue()


class FailOpenTests(HookIsolationMixin, unittest.TestCase):
    """No input and no internal failure may produce a non-zero exit."""

    def test_hostile_payloads_exit_zero(self):
        for provider in PROVIDERS:
            for name, payload in HOSTILE_PAYLOADS.items():
                with self.subTest(provider=provider, payload=name):
                    code, _ = run_hook(provider, self.log_path, payload)
                    self.assertEqual(0, code)

    def test_unknown_provider_exits_zero(self):
        code, _ = run_hook("not-a-provider", self.log_path, '{"hook_event_name": "Stop"}')
        self.assertEqual(0, code)

    def test_unwritable_log_path_exits_zero(self):
        """A read-only or impossible log destination must not break the agent."""
        blocked = self.home / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)
        self.addCleanup(blocked.chmod, 0o700)
        code, _ = run_hook("claude", blocked / "sub" / "hook.jsonl", '{"hook_event_name": "Stop"}')
        self.assertEqual(0, code)

    def test_log_path_is_a_directory_exits_zero(self):
        directory = self.home / "iam-a-directory"
        directory.mkdir()
        code, _ = run_hook("claude", directory, '{"hook_event_name": "Stop"}')
        self.assertEqual(0, code)

    def test_every_internal_failure_exits_zero(self):
        """Whatever blows up downstream, the hook still reports success.

        Each collaborator is replaced with one that raises, one at a time, so
        a future refactor that lets an exception escape gets caught here.
        """
        collaborators = [
            "routed_hook_payload",
            "send_hook_event",
            "write_hook_line",
            "write_hook_status_audit",
            "hook_event_socket_disabled",
        ]
        for name in collaborators:
            with self.subTest(raises=name):
                with patch.object(
                    hook_module, name, side_effect=RuntimeError(f"boom in {name}")
                ):
                    code, _ = run_hook(
                        "claude", self.log_path, '{"hook_event_name": "Stop"}'
                    )
                    self.assertEqual(0, code, f"{name} raising must not fail the hook")

    def test_interrupts_are_not_swallowed(self):
        """Fail-open covers errors, not Ctrl-C.

        ``hook_log_main`` catches ``Exception``, deliberately not
        ``BaseException``: swallowing an interrupt would make the hook
        unkillable. The shell wrapper (``; true``) is what keeps an
        interrupted hook from failing the agent turn -- see
        ``InstalledCommandTests``.
        """
        with patch.object(
            hook_module, "routed_hook_payload", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_hook("claude", self.log_path, "{}")

    def test_socket_send_failure_still_writes_log(self):
        """The log is the durable record; a dead status bar must not lose it."""
        with patch.dict(os.environ, {"SIDEPULSE_DISABLE_EVENT_SOCKET": "0"}):
            with patch.object(
                hook_module, "send_hook_event", side_effect=OSError("no listener")
            ):
                code, _ = run_hook(
                    "claude", self.log_path, '{"hook_event_name": "Stop"}'
                )
        self.assertEqual(0, code)
        self.assertTrue(self.log_path.exists())
        self.assertEqual(
            json.loads(self.log_path.read_text())["hook_event_name"],
            "Stop",
        )


class StdoutPurityTests(HookIsolationMixin, unittest.TestCase):
    """Hook stdout belongs to the agent runtime, not to us."""

    def test_hooks_write_nothing_to_stdout(self):
        for provider in PROVIDERS:
            for name, payload in HOSTILE_PAYLOADS.items():
                with self.subTest(provider=provider, payload=name):
                    _, out = run_hook(provider, self.log_path, payload)
                    self.assertEqual("", out, f"hook printed to stdout: {out!r}")

    def test_hooks_write_nothing_to_stdout_on_internal_error(self):
        with patch.object(hook_module, "write_hook_line", side_effect=RuntimeError("boom")):
            _, out = run_hook("claude", self.log_path, '{"hook_event_name": "Stop"}')
        self.assertEqual("", out)


class HappyPathTests(HookIsolationMixin, unittest.TestCase):
    """Fail-open is worthless if the hook never succeeds at anything."""

    def test_valid_payload_is_appended_as_jsonl(self):
        payload = json.dumps({"hook_event_name": "Stop", "session_id": "s1"})
        code, _ = run_hook("claude", self.log_path, payload)
        self.assertEqual(0, code)
        self.assertTrue(self.log_path.exists(), "hook wrote no log file")
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(lines))
        record = json.loads(lines[0])
        self.assertEqual("Stop", record["hook_event_name"])
        self.assertIn("logged_at", record)

    def test_repeated_calls_append_rather_than_truncate(self):
        for index in range(5):
            payload = json.dumps({"hook_event_name": "Stop", "session_id": f"s{index}"})
            run_hook("claude", self.log_path, payload)
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(5, len(lines))
        for line in lines:
            json.loads(line)  # every line must be independently parseable

    def test_each_record_is_one_physical_line(self):
        """Multi-line content must not break the JSONL reader downstream."""
        payload = json.dumps({"hook_event_name": "Stop", "message": "one\ntwo\nthree"})
        run_hook("claude", self.log_path, payload)
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(lines))
        self.assertEqual("one\ntwo\nthree", json.loads(lines[0])["message"])

    def test_malformed_payload_is_recorded_as_parse_error(self):
        """Garbage in must still leave a breadcrumb rather than vanishing."""
        run_hook("claude", self.log_path, "definitely not json")
        record = json.loads(self.log_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual("ParseError", record["hook_event_name"])
        self.assertIn("definitely not json", record["raw"])

    def test_format_hook_payload_always_timestamps(self):
        for name, payload in HOSTILE_PAYLOADS.items():
            for provider in PROVIDERS:
                with self.subTest(provider=provider, payload=name):
                    line = format_hook_payload(provider, payload)
                    self.assertIsInstance(line, dict)
                    self.assertTrue(line.get("logged_at"))

    def test_routed_payload_returns_usable_triple(self):
        for name, payload in HOSTILE_PAYLOADS.items():
            with self.subTest(payload=name):
                provider, path, line = routed_hook_payload(
                    "claude", self.log_path, payload
                )
                self.assertIsInstance(provider, str)
                self.assertTrue(provider)
                self.assertIsInstance(path, Path)
                self.assertIsInstance(line, dict)


class EntryPointSubprocessTests(unittest.TestCase):
    """The hook runs as a fresh process from a shell command, so test it that way.

    This is what catches packaging and sys.path regressions that in-process
    tests cannot see -- including the legacy module names still referenced by
    hook configs installed before the SidePulse rename.
    """

    ENTRY_POINTS = (
        "sidepulse/hook_entry.py",
        "sidepulse_cli/hook_entry.py",
        "agent_monitor/hook_entry.py",
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def run_entry(self, relative: str, args: list[str], payload: str = "{}"):
        env = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "SIDEPULSE_DISABLE_EVENT_SOCKET": "1",
        }
        # Deliberately no PYTHONPATH: the entry points must bootstrap their
        # own sys.path exactly as they do when a hook config invokes them.
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            [sys.executable, str(SRC_ROOT / relative), *args],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=str(self.home),
        )

    def test_entry_points_log_and_exit_zero(self):
        for relative in self.ENTRY_POINTS:
            with self.subTest(entry_point=relative):
                log_path = self.home / f"{relative.replace('/', '_')}.jsonl"
                result = self.run_entry(
                    relative,
                    ["--provider", "claude", "--log", str(log_path)],
                    json.dumps({"hook_event_name": "Stop", "session_id": "s1"}),
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("", result.stdout)
                self.assertTrue(log_path.exists(), f"{relative} wrote no log")

    def test_entry_points_survive_bad_arguments(self):
        bad_arguments = [
            [],
            ["--provider"],
            ["--provider", "claude"],
            ["--log", "/tmp/x.jsonl"],
            ["--provider", "claude", "--log"],
            ["--unknown-flag", "value"],
        ]
        for relative in self.ENTRY_POINTS:
            for args in bad_arguments:
                with self.subTest(entry_point=relative, args=args):
                    result = self.run_entry(relative, args)
                    self.assertEqual(0, result.returncode, result.stderr)

    def test_entry_points_survive_hostile_payloads(self):
        for name, payload in HOSTILE_PAYLOADS.items():
            with self.subTest(payload=name):
                result = self.run_entry(
                    "sidepulse/hook_entry.py",
                    ["--provider", "claude", "--log", str(self.home / "h.jsonl")],
                    payload,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("", result.stdout)

    def test_entry_point_exits_promptly(self):
        """A hook that hangs stalls every tool call in the agent session."""
        result = self.run_entry(
            "sidepulse/hook_entry.py",
            ["--provider", "claude", "--log", str(self.home / "h.jsonl")],
            json.dumps({"hook_event_name": "Stop"}),
        )
        # subprocess.run raises TimeoutExpired past 30s; reaching here is the
        # assertion. Keep the explicit check so the intent is not lost.
        self.assertEqual(0, result.returncode)

    def test_hook_works_without_the_gui_stack(self):
        """Hooks must not depend on PyObjC.

        The hook path runs in the user's agent session, which may have no
        working AppKit at all -- a broken PyObjC install, a non-macOS host, or
        exactly the missing-framework situation that motivated these tests.
        Logging must keep working regardless.
        """
        log_path = self.home / "no-gui.jsonl"
        blocker = self.home / "run_with_gui_blocked.py"
        blocker.write_text(
            "import sys, runpy\n"
            "BLOCKED = {'AppKit', 'Foundation', 'objc', 'Quartz',\n"
            "           'ScriptingBridge', 'WebKit', 'JavaScriptCore'}\n"
            "class Blocker:\n"
            "    def find_module(self, name, path=None):\n"
            "        return self if name.split('.')[0] in BLOCKED else None\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in BLOCKED:\n"
            "            raise ImportError(f'{name} blocked for test')\n"
            "        return None\n"
            "sys.meta_path.insert(0, Blocker())\n"
            "for name in list(sys.modules):\n"
            "    if name.split('.')[0] in BLOCKED:\n"
            "        del sys.modules[name]\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n",
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "HOME": str(self.home),
            "SIDEPULSE_DISABLE_EVENT_SOCKET": "1",
        }
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [
                sys.executable,
                str(blocker),
                str(SRC_ROOT / "sidepulse/hook_entry.py"),
                "--provider", "claude",
                "--log", str(log_path),
            ],
            input=json.dumps({"hook_event_name": "Stop", "session_id": "s1"}),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=str(self.home),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(
            log_path.exists(),
            f"hook logged nothing with PyObjC blocked; stderr={result.stderr}",
        )
        record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual("Stop", record["hook_event_name"])

    # A hook config in the wild can name any of these. `sidepulse hook-log`
    # is what older installs wrote; `agent-monitor hook-log` is what the
    # frozen-app command writes; `python -m sidepulse` reaches whichever of
    # the two CLIs __main__ dispatches to. All of them must log.
    CLI_ROUTES = (
        ["hook-log"],
        ["agent-monitor", "hook-log"],
    )

    def test_cli_hook_log_subcommand_matches_entry_point(self):
        for index, route in enumerate(self.CLI_ROUTES):
            with self.subTest(route=" ".join(route)):
                log_path = self.home / f"cli-{index}.jsonl"
                env = {
                    **os.environ,
                    "HOME": str(self.home),
                    "SIDEPULSE_DISABLE_EVENT_SOCKET": "1",
                }
                result = subprocess.run(
                    [
                        sys.executable, "-m", "sidepulse", *route,
                        "--provider", "claude", "--log", str(log_path),
                    ],
                    input=json.dumps({"hook_event_name": "Stop"}),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=env,
                    cwd=str(REPO_ROOT),
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertTrue(log_path.exists())
                # Catches stray debug prints at module scope, which would
                # corrupt the hook protocol for anyone routing hooks through
                # the CLI.
                self.assertEqual(
                    "", result.stdout, f"CLI printed to stdout: {result.stdout!r}"
                )

    def test_both_cli_parsers_accept_hook_log(self):
        """`sidepulse` and `agent-monitor` are separate parsers.

        They drifted apart once already: hook-log existed only on
        `agent-monitor`, so `sidepulse hook-log` -- the command older installs
        wrote into agent configs -- died with argparse exit code 2.
        """
        for build in (build_parser, build_sidepulse_parser):
            for provider in PROVIDERS:
                with self.subTest(parser=build.__name__, provider=provider):
                    args = build().parse_args(
                        ["hook-log", "--provider", provider, "--log", "/tmp/x.jsonl"]
                    )
                    self.assertEqual(provider, args.provider)
                    self.assertEqual(Path("/tmp/x.jsonl"), args.log)
                    self.assertIs(cmd_hook_log, args.func)


class InstalledCommandTests(unittest.TestCase):
    """The string we write into somebody's agent config must be safe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_command_is_shell_fail_open(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                command = hook_command(provider, self.home / "log.jsonl")
                self.assertTrue(
                    command.rstrip().endswith("; true"),
                    f"hook command is not fail-open: {command}",
                )

    def test_command_quotes_paths_with_spaces(self):
        import shlex

        weird = self.home / "path with spaces" / "log's file.jsonl"
        command = hook_command("claude", weird)
        parts = shlex.split(command)
        self.assertIn(str(weird), parts, "log path was not quoted intact")

    def test_command_exits_zero_when_python_is_missing(self):
        """Config survives a deleted virtualenv; the agent must keep working."""
        command = hook_command(
            "claude", self.home / "log.jsonl", python_executable="/nonexistent/python"
        )
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30,
            input="{}",
        )
        self.assertEqual(
            0, result.returncode, f"stale hook command failed the turn: {command}"
        )

    def test_installed_command_actually_logs(self):
        log_path = self.home / "installed.jsonl"
        command = hook_command("claude", log_path)
        result = subprocess.run(
            command,
            shell=True,
            input=json.dumps({"hook_event_name": "Stop"}),
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "SIDEPULSE_DISABLE_EVENT_SOCKET": "1"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(log_path.exists(), "installed hook command wrote nothing")

    def test_fail_open_wrapper_neutralizes_failure(self):
        # Note: a bare `exit 7` would terminate the shell before `; true` runs.
        # Hooks invoke a real program, which is what these cases model.
        failing_commands = (
            "/usr/bin/false",
            "/nonexistent/program --flag",
            "/bin/sh -c 'exit 7'",
        )
        for command in failing_commands:
            with self.subTest(command=command):
                result = subprocess.run(
                    fail_open_command(command),
                    shell=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
