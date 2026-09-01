"""Clean-room environment tests: install the project the way a user does.

Every other test in this repo runs inside the developer's environment, which
accumulates packages that were installed by hand. That is precisely how the
missing ``pyobjc-framework-ScriptingBridge`` dependency stayed invisible: the
framework was sitting in the working virtualenv, so imports succeeded locally
while a fresh install was broken.

These tests build an empty virtualenv, run ``pip install .`` against nothing
but ``pyproject.toml``, and then exercise the result. A dependency that is not
declared is simply absent, so the failure is real rather than theoretical.

The same tests double as a post-publish smoke test. Set
``SIDEPULSE_INSTALL_SPEC`` to a requirement string
(``sidepulse==1.20260901.67530``) and
they install that from PyPI instead of the local tree, which checks the
artifact users actually download rather than the one built here.

They are slower than the rest of the suite (a few seconds with a warm wheel
cache, longer on a cold one). Set ``SIDEPULSE_SKIP_CLEAN_INSTALL=1`` to skip
them while iterating locally; CI always runs them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The error the status bar prints when PyObjC imports fail. If a clean install
# ever produces this, the dependency list is wrong again.
DEPENDENCY_ERROR_MARKER = "requires PyObjC"

# Substrings that mean pip could not reach an index, as opposed to the package
# being genuinely broken. Only these downgrade a failure to a skip.
NETWORK_FAILURE_MARKERS = (
    "Temporary failure in name resolution",
    "Network is unreachable",
    "Could not find a version",
    "No matching distribution",
    "Failed to establish a new connection",
    "Connection refused",
    "Read timed out",
    "retries exceeded",
)


def install_spec() -> str:
    """What to hand to pip: the local tree by default, or a published release."""
    return os.environ.get("SIDEPULSE_INSTALL_SPEC") or str(REPO_ROOT)


def is_local_install() -> bool:
    return not os.environ.get("SIDEPULSE_INSTALL_SPEC")


def expected_version() -> str:
    """The version the install is expected to report.

    Taken from the requirement pin when smoke-testing a release. Local builds
    use a tag-derived development version, so only internal agreement matters.
    """
    spec = os.environ.get("SIDEPULSE_INSTALL_SPEC")
    if spec and "==" in spec:
        return spec.split("==", 1)[1].strip()
    return ""


@unittest.skipIf(
    os.environ.get("SIDEPULSE_SKIP_CLEAN_INSTALL") == "1",
    "clean-install tests disabled via SIDEPULSE_SKIP_CLEAN_INSTALL",
)
class CleanInstallTests(unittest.TestCase):
    """Install into an empty virtualenv and exercise what a user would get."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="sidepulse-clean-env-")
        root = Path(cls._tmp.name)
        cls.venv = root / "venv"
        cls.home = root / "home"
        cls.home.mkdir()

        # No --system-site-packages: the environment starts empty, so the only
        # things importable are what pyproject.toml declares.
        create = subprocess.run(
            [sys.executable, "-m", "venv", str(cls.venv)],
            capture_output=True, text=True, timeout=300,
        )
        if create.returncode != 0:
            cls._tmp.cleanup()
            raise unittest.SkipTest(f"could not create virtualenv: {create.stderr}")

        cls.python = cls.venv / "bin" / "python"
        cls.install_spec = install_spec()

        install = subprocess.run(
            [
                str(cls.python), "-m", "pip", "install",
                "--quiet", "--disable-pip-version-check", cls.install_spec,
            ],
            capture_output=True, text=True, timeout=900,
        )
        if install.returncode != 0:
            output = install.stderr + install.stdout
            # A published release that cannot be installed is a real failure,
            # not an environment quirk, so only the local case degrades to a
            # skip when there is no index.
            if is_local_install() and any(
                marker in output for marker in NETWORK_FAILURE_MARKERS
            ):
                cls._tmp.cleanup()
                raise unittest.SkipTest(f"no package index reachable: {output[-500:]}")
            cls._tmp.cleanup()
            raise AssertionError(
                f"`pip install {cls.install_spec}` failed on a clean "
                f"environment:\n{output}"
            )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def env(self) -> dict:
        """A minimal environment: no PYTHONPATH leaking the source tree in."""
        env = {
            "PATH": f"{self.venv / 'bin'}:{os.environ.get('PATH', '')}",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "SIDEPULSE_DISABLE_EVENT_SOCKET": "1",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        return env

    def run_python(self, code: str, **kwargs) -> subprocess.CompletedProcess:
        """Run code with the installed interpreter, outside the source tree.

        cwd matters: from REPO_ROOT, `import sidepulse` could resolve against
        ./src rather than the installed package and prove nothing.
        """
        return subprocess.run(
            [str(self.python), "-c", textwrap.dedent(code)],
            capture_output=True, text=True, timeout=300,
            env=self.env(), cwd=str(self.home), **kwargs,
        )

    def run_script(self, name: str, args: list[str], **kwargs):
        return subprocess.run(
            [str(self.venv / "bin" / name), *args],
            capture_output=True, text=True, timeout=300,
            env=self.env(), cwd=str(self.home), **kwargs,
        )

    def shipped_modules(self) -> list[str]:
        names = []
        for path in sorted((REPO_ROOT / "src").rglob("*.py")):
            rel = path.relative_to(REPO_ROOT / "src")
            if rel.name == "__main__.py":
                continue  # runs the CLI on import by design
            parts = list(rel.parts)
            parts[-1] = rel.stem
            if parts[-1] == "__init__":
                parts.pop()
            if parts:
                names.append(".".join(parts))
        return names

    # -- tests -----------------------------------------------------------

    def test_installed_package_is_not_the_source_tree(self):
        """Guard the guard: these tests are worthless if they import ./src."""
        result = self.run_python(
            "import sidepulse, sys; sys.stdout.write(sidepulse.__file__)"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(
            "site-packages",
            result.stdout,
            f"tests imported {result.stdout} instead of the installed package",
        )

    def test_installed_version_is_the_expected_one(self):
        """Post-publish, this proves PyPI is serving the release we tagged."""
        result = self.run_python(
            """
            import sys
            from importlib.metadata import version
            import sidepulse
            sys.stdout.write(f"{version('sidepulse')} {sidepulse.__version__}")
            """
        )
        self.assertEqual(0, result.returncode, result.stderr)
        metadata_version, dunder_version = result.stdout.split()
        expected = expected_version()
        if expected:
            self.assertEqual(
                expected,
                metadata_version,
                f"installed {self.install_spec} reports version {metadata_version}",
            )
        self.assertEqual(
            metadata_version,
            dunder_version,
            "wheel metadata and sidepulse.__version__ disagree",
        )

    def test_status_bar_imports_after_plain_install(self):
        """The regression test for the ScriptingBridge dependency.

        `pip install .` and nothing else must be enough to import the status
        bar. No manual `pip install pyobjc-framework-...` may be required.
        """
        if sys.platform != "darwin":
            self.skipTest("status bar is macOS only")
        result = self.run_python("import sidepulse.status_bar")
        self.assertNotIn(
            DEPENDENCY_ERROR_MARKER,
            result.stderr + result.stdout,
            "a clean install still reports missing PyObjC dependencies:\n"
            f"{result.stderr}",
        )
        self.assertEqual(
            0,
            result.returncode,
            f"importing the status bar failed on a clean install:\n{result.stderr}",
        )

    def test_every_shipped_module_imports_after_plain_install(self):
        darwin_only = {
            "sidepulse.status_bar",
            "sidepulse.virtual_device",
            "sidepulse.led_wasm",
        }
        for name in self.shipped_modules():
            if name in darwin_only and sys.platform != "darwin":
                continue
            with self.subTest(module=name):
                result = self.run_python(f"import {name}")
                self.assertEqual(
                    0,
                    result.returncode,
                    f"{name} does not import on a clean install:\n{result.stderr}",
                )

    def test_declared_frameworks_are_actually_installed(self):
        """Each PyObjC framework the code imports must arrive with the package."""
        if sys.platform != "darwin":
            self.skipTest("PyObjC frameworks are macOS only")
        for module in ("objc", "AppKit", "Foundation", "Quartz", "ScriptingBridge"):
            with self.subTest(framework=module):
                result = self.run_python(f"import {module}")
                self.assertEqual(
                    0,
                    result.returncode,
                    f"{module} is missing from a clean install; "
                    f"add its distribution to pyproject.toml dependencies",
                )

    def test_console_scripts_are_installed(self):
        expected = ("sidepulse", "agent-monitor", "agent-status-bar", "sidepulse-reply")
        for name in expected:
            with self.subTest(script=name):
                self.assertTrue(
                    (self.venv / "bin" / name).exists(),
                    f"console script {name} was not installed",
                )

    def test_console_scripts_run(self):
        for name in ("sidepulse", "agent-monitor", "sidepulse-reply"):
            with self.subTest(script=name):
                result = self.run_script(name, ["--help"])
                self.assertIn(result.returncode, (0, 2), result.stderr)
                self.assertNotIn(DEPENDENCY_ERROR_MARKER, result.stderr)

    def test_packaged_resources_are_installed(self):
        """Package data declared in pyproject must survive the wheel build."""
        result = self.run_python(
            """
            import pathlib, sidepulse
            root = pathlib.Path(sidepulse.__file__).parent
            print("\\n".join(sorted(str(p.relative_to(root))
                                   for p in root.rglob("*")
                                   if p.is_file() and p.suffix != ".py")))
            """
        )
        self.assertEqual(0, result.returncode, result.stderr)
        source_resources = {
            p.name
            for p in (REPO_ROOT / "src/sidepulse/resources").rglob("*")
            if p.is_file()
            and p.suffix != ".py"
            and not p.name.startswith(".")
            and "__pycache__" not in p.parts
        }
        self.assertTrue(source_resources, "expected packaged resource files")
        for name in sorted(source_resources):
            with self.subTest(resource=name):
                self.assertIn(
                    name,
                    result.stdout,
                    f"{name} is in the source tree but missing from the install",
                )

    def test_hook_works_from_the_installed_package(self):
        """The hook path must work end to end on a fresh install."""
        log_path = self.home / "hook.jsonl"
        result = subprocess.run(
            [
                str(self.venv / "bin" / "agent-monitor"), "hook-log",
                "--provider", "claude", "--log", str(log_path),
            ],
            input=json.dumps({"hook_event_name": "Stop", "session_id": "s1"}),
            capture_output=True, text=True, timeout=300,
            env=self.env(), cwd=str(self.home),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        self.assertTrue(log_path.exists(), "installed hook wrote no log")
        record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual("Stop", record["hook_event_name"])

    def test_installed_hook_command_points_at_the_installed_package(self):
        """`sidepulse install` writes a command into somebody's agent config.

        On a clean install that command must reference the installed package
        and run successfully, not a path that only exists on a dev machine.
        """
        result = self.run_python(
            """
            from pathlib import Path
            from sidepulse.install import hook_command
            print(hook_command("claude", Path.home() / "installed-hook.jsonl"))
            """
        )
        self.assertEqual(0, result.returncode, result.stderr)
        command = result.stdout.strip()
        self.assertTrue(command, "hook_command produced nothing")

        run = subprocess.run(
            command, shell=True,
            input=json.dumps({"hook_event_name": "Stop"}),
            capture_output=True, text=True, timeout=300,
            env=self.env(), cwd=str(self.home),
        )
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertTrue(
            (self.home / "installed-hook.jsonl").exists(),
            f"the command written into agent configs logged nothing: {command}",
        )


    @unittest.skipUnless(sys.platform == "darwin", "status bar is macOS only")
    def test_status_bar_start_foreground_gets_past_startup(self):
        """`sidepulse status-bar start --foreground` must not die on imports.

        This is the literal command that failed, run against a clean install.
        It enters a GUI event loop, so success looks like "still running when
        we kill it"; failure looks like exiting immediately with the PyObjC
        dependency error.
        """
        # Mark first-run setup complete so launching does not open a window on
        # the developer's screen.
        config_dir = self.home / ".config/sidepulse/agent-monitor"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "settings.json").write_text(
            json.dumps({"setup_screen_completed": True}), encoding="utf-8"
        )

        process = subprocess.Popen(
            [
                str(self.venv / "bin" / "sidepulse"),
                "status-bar", "start", "--foreground",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.env(), cwd=str(self.home),
        )
        try:
            stdout, stderr = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            # Still alive after 8s: it got past imports and is running.
            process.kill()
            stdout, stderr = process.communicate()
            self.assertNotIn(DEPENDENCY_ERROR_MARKER, stdout + stderr)
            return

        output = stdout + stderr
        if DEPENDENCY_ERROR_MARKER in output:
            self.fail(
                "status-bar died on missing dependencies from a clean "
                f"install:\n{output}"
            )
        if os.environ.get("SIDEPULSE_REQUIRE_UI_TESTS") == "1":
            self.fail(
                "status-bar exited immediately instead of running.\n"
                f"returncode={process.returncode}\nstdout={stdout}\nstderr={stderr}"
            )
        # Exited for some other reason -- most likely no window server, which
        # is an environment limitation rather than a packaging defect. The
        # dependency check above still ran, which is what this test is for.
        self.skipTest(
            f"status-bar could not run a GUI here (rc={process.returncode}): "
            f"{output.strip()[:300]}"
        )


if __name__ == "__main__":
    unittest.main()
