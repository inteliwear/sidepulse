#!/usr/bin/env python3
"""Check that SidePulse still works after a Hermes update.

Run this after upgrading Hermes (or any time the LEDs look wrong):

    python3 integrations/hermes/scripts/check_hermes_compat.py
    hermes sidepulse compat

Exit 0 if the contract is intact. Exit 1 if something needs a fix.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_HOOKS = (
    "on_session_start",
    "on_session_activate",
    "on_session_reset",
    "pre_llm_call",
    "pre_tool_call",
    "post_tool_call",
    "pre_approval_request",
    "post_approval_response",
    "api_request_error",
    "post_llm_call",
    "on_session_end",
)

PLUGIN_NAME = "hermes-sidepulse"
DEFAULT_BASELINE = (
    Path.home() / ".local" / "state" / "sidepulse" / "agent-monitor" / "hermes-compat-baseline.json"
)


def _run(command: list[str], timeout: int = 20) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "", f"command not found: {command[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out: {' '.join(command)}"
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _json_load(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def discover_plugin_yaml() -> Path | None:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "plugin.yaml",
        Path.home() / ".hermes" / "plugins" / PLUGIN_NAME / "plugin.yaml",
        _home() / "plugins" / PLUGIN_NAME / "plugin.yaml",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def hooks_from_plugin_yaml(path: Path | None) -> list[str]:
    if path is None or not path.is_file():
        return list(REQUIRED_HOOKS)
    hooks: list[str] = []
    in_hooks = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.startswith("hooks:"):
            in_hooks = True
            continue
        if in_hooks:
            stripped = line.strip()
            if stripped.startswith("- "):
                hooks.append(stripped[2:].strip())
                continue
            if stripped and not stripped.startswith("#"):
                break
    return hooks or list(REQUIRED_HOOKS)


def hermes_version() -> dict[str, Any]:
    code, out, err = _run(["hermes", "--version"])
    text = "\n".join(part for part in (out, err) if part)
    version = ""
    install_dir = ""
    for line in text.splitlines():
        if line.startswith("Hermes Agent "):
            version = line.replace("Hermes Agent ", "").strip()
        if line.startswith("Install directory:"):
            install_dir = line.split(":", 1)[1].strip()
    return {
        "ok": code == 0 and bool(version),
        "version": version,
        "install_dir": install_dir,
        "error": "" if code == 0 else (err or out or f"exit {code}"),
    }


def live_valid_hooks(install_dir: str) -> dict[str, Any]:
    python = Path(install_dir) / ".venv" / "bin" / "python" if install_dir else None
    if python and python.is_file():
        code, out, err = _run(
            [
                str(python),
                "-c",
                "from hermes_cli.plugins import VALID_HOOKS; print('\\n'.join(sorted(VALID_HOOKS)))",
            ]
        )
        hooks = [line.strip() for line in out.splitlines() if line.strip()]
        return {
            "ok": code == 0 and bool(hooks),
            "hooks": hooks,
            "error": "" if code == 0 and hooks else (err or out or "could not import VALID_HOOKS"),
        }
    return {"ok": False, "hooks": [], "error": "Hermes venv python not found"}


def plugin_status() -> dict[str, Any]:
    code, out, err = _run(["hermes", "plugins", "list", "--no-bundled", "--json"])
    payload = _json_load(out)
    plugins = payload if isinstance(payload, list) else []
    match = next(
        (item for item in plugins if isinstance(item, dict) and item.get("name") == PLUGIN_NAME),
        None,
    )
    return {
        "ok": code == 0 and match is not None and match.get("status") == "enabled",
        "plugin": match,
        "error": ""
        if code == 0 and match is not None
        else (err or out or f"{PLUGIN_NAME} not listed"),
    }


def socket_status() -> dict[str, Any]:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    path = base / "sidepulse" / "agent-monitor" / "events.sock"
    exists = path.exists()
    connected = False
    if exists:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.2)
        try:
            client.connect(str(path))
            connected = True
        except OSError:
            connected = False
        finally:
            client.close()
    return {
        "ok": exists and connected,
        "path": str(path),
        "exists": exists,
        "connected": connected,
        "error": "" if exists and connected else "status-bar socket is not accepting connections",
    }


def sidepulse_cli_status() -> dict[str, Any]:
    reports: dict[str, Any] = {}
    all_ok = True
    for mode in ("working", "ask", "done"):
        code, out, err = _run(
            ["hermes", "sidepulse", "test", "--mode", mode, "--json"],
            timeout=25,
        )
        payload = _json_load(out) if code == 0 else None
        ok = bool(
            payload
            and payload.get("mode")
            and payload.get("logged") is True
        )
        reports[mode] = {
            "ok": ok,
            "exit_code": code,
            "report": payload,
            "error": "" if ok else (err or out or f"test --mode {mode} failed"),
        }
        all_ok = all_ok and ok
    return {"ok": all_ok, "modes": reports}


def compare_hooks(required: list[str], live: list[str]) -> dict[str, Any]:
    required_set = set(required)
    live_set = set(live)
    missing = sorted(required_set - live_set)
    extra = sorted(live_set - required_set)
    return {
        "ok": not missing,
        "required": required,
        "live": sorted(live_set),
        "missing": missing,
        "extra": extra,
    }


def load_baseline(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = _json_load(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def write_baseline(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hermes_version": report["hermes"]["version"],
        "hooks": report["hooks"]["live"],
        "plugin": report["plugin"]["plugin"],
    }
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_report(*, baseline_path: Path) -> dict[str, Any]:
    plugin_yaml = discover_plugin_yaml()
    required = hooks_from_plugin_yaml(plugin_yaml)
    hermes = hermes_version()
    hooks_live = live_valid_hooks(hermes.get("install_dir") or "")
    hooks = compare_hooks(required, hooks_live.get("hooks") or [])
    if not hooks_live.get("ok"):
        hooks["ok"] = False
        hooks["error"] = hooks_live.get("error")
    plugin = plugin_status()
    sock = socket_status()
    tests = sidepulse_cli_status()
    baseline = load_baseline(baseline_path)
    drift: dict[str, Any] = {"ok": True, "version_changed": False, "hooks_removed": [], "hooks_added": []}
    if baseline:
        previous_hooks = set(baseline.get("hooks") or [])
        current_hooks = set(hooks.get("live") or [])
        drift["version_changed"] = bool(
            baseline.get("hermes_version") and baseline.get("hermes_version") != hermes.get("version")
        )
        drift["hooks_removed"] = sorted(previous_hooks - current_hooks)
        drift["hooks_added"] = sorted(current_hooks - previous_hooks)
        if any(name in REQUIRED_HOOKS for name in drift["hooks_removed"]):
            drift["ok"] = False

    checks = {
        "hermes": hermes["ok"],
        "hooks": hooks["ok"],
        "plugin": plugin["ok"],
        "socket": sock["ok"],
        "synthetic_events": tests["ok"],
        "baseline_drift": drift["ok"],
    }
    return {
        "ok": all(checks.values()),
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plugin_yaml": str(plugin_yaml) if plugin_yaml else "",
        "checks": checks,
        "hermes": hermes,
        "hooks": hooks,
        "plugin": plugin,
        "socket": sock,
        "synthetic_events": tests,
        "baseline": {"path": str(baseline_path), "previous": baseline, "drift": drift},
    }


def print_human(report: dict[str, Any]) -> None:
    status = "OK" if report["ok"] else "NEEDS FIX"
    print(f"SidePulse Hermes compatibility: {status}")
    print(f"  Hermes: {report['hermes'].get('version') or report['hermes'].get('error')}")
    print(f"  plugin: {((report['plugin'].get('plugin') or {}).get('status') or 'missing')}")
    print(f"  socket: {'up' if report['socket']['ok'] else 'down'} ({report['socket']['path']})")
    missing = report["hooks"].get("missing") or []
    if missing:
        print(f"  missing hooks: {', '.join(missing)}")
    else:
        print(f"  required hooks: {len(report['hooks'].get('required') or REQUIRED_HOOKS)} present")
    for mode, result in (report["synthetic_events"].get("modes") or {}).items():
        mark = "ok" if result.get("ok") else "FAIL"
        delivered = ((result.get("report") or {}).get("delivered"))
        print(f"  test {mode}: {mark} (socket={'delivered' if delivered else 'not delivered'})")
    drift = report["baseline"]["drift"]
    previous = report["baseline"]["previous"]
    if previous and drift.get("version_changed"):
        print(
            f"  Hermes version changed: {previous.get('hermes_version')} -> {report['hermes'].get('version')}"
        )
    if drift.get("hooks_removed"):
        print(f"  hooks removed since last baseline: {', '.join(drift['hooks_removed'])}")
    if not report["ok"]:
        print("\nFix the failing checks above before trusting SidePulse after this Hermes update.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="Baseline file used to detect hook/version drift",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Write the current Hermes version and hook set as the new baseline",
    )
    args = parser.parse_args(argv)
    report = build_report(baseline_path=args.baseline)
    if args.update_baseline and report["hooks"].get("live"):
        write_baseline(args.baseline, report)
        report["baseline"]["updated"] = True
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
