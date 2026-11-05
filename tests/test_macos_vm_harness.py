"""Harness checks must not accept stale evidence or hooks that silently do nothing."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location("vm_" + name, ROOT / "scripts/macos-vm" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load("check")
host = load("host")


@pytest.mark.parametrize("status", ["started", "suspended"])
def test_clone_rejects_non_stopped_source(tmp_path, monkeypatch, status):
    monkeypatch.setattr(host, "utm_inventory", lambda: [dict(uuid="source", name="Clean", status=status)])
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: pytest.fail("must not mutate running VM"))
    with pytest.raises(ValueError, match="Shut down"):
        host.clone_stopped_vm("Clean", "Baseline", tmp_path)


def test_clone_verifies_new_identity_and_preserves_source(tmp_path, monkeypatch):
    source = dict(uuid="source", name="Clean", status="stopped")
    clone = dict(uuid="new", name="Baseline", status="stopped")
    inventories = iter([[source], [source, clone]])
    monkeypatch.setattr(host, "utm_inventory", lambda: next(inventories))
    calls = []
    monkeypatch.setattr(host.subprocess, "run", lambda argv, **kw: calls.append(argv))
    record = host.clone_stopped_vm("Clean", "Baseline", tmp_path)
    assert calls == [[host.UTMCTL, "clone", "source", "--name", "Baseline"]]
    assert record["source"] == source and record["clone"] == clone
    assert json.loads((tmp_path / "clone-new.json").read_text()) == record


def evidence(cwd):
    records = [{"cwd": str(cwd), "session_id": "new", "hook_event_name": event}
               for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")]
    snapshots = [{"snapshot": {"statuses": [{"provider": "claude", "session_id": "new", "mode": mode}]}}
                 for mode in ("working", "completed")]
    return records, snapshots


def test_legacy_comparison_observes_persisted_state_without_replaying_hooks(tmp_path):
    path = tmp_path / "latest.json"
    rows = [{"provider": "codex", "session_id": "live", "mode": "tool_running"}]
    path.write_text(json.dumps({"updated_at": "2026-10-27T05:00:00Z", "statuses": rows}))
    reply = check.legacy_snapshot(path)
    assert reply["evidence_source"] == "legacy-live-monitor-file"
    assert check.state_rows(reply) == rows
    path.write_text('{"statuses": []}')
    with pytest.raises(ValueError, match="Invalid"):
        check.legacy_snapshot(path)


def test_live_requires_this_workspace_session_and_completion(tmp_path):
    records, snapshots = evidence(tmp_path)
    assert check.live_evidence("claude", records, snapshots, tmp_path, "token", "token")["sessions"] == ["new"]
    with pytest.raises(AssertionError, match="installed hooks"):
        check.live_evidence("claude", records, snapshots, tmp_path / "other", "token", "token")
    snapshots[-1]["snapshot"]["statuses"][0]["session_id"] = "old"
    with pytest.raises(AssertionError, match="Daemon"):
        check.live_evidence("claude", records, snapshots, tmp_path, "token", "token")


def test_live_requires_actual_fixture_read(tmp_path):
    records, snapshots = evidence(tmp_path)
    with pytest.raises(AssertionError, match="fixture token"):
        check.live_evidence("claude", records, snapshots, tmp_path, "secret-fixture", "done")


def test_replay_waits_for_legacy_state_file_and_same_session(tmp_path, monkeypatch):
    harness = check.Harness(tmp_path / "result")
    expected = {"snapshot": {"statuses": [{"provider": "claude", "session_id": "new", "mode": "working"}]}}
    replies = iter([FileNotFoundError("not written yet"),
                    json.JSONDecodeError("partial write", "{", 1),
                    {"snapshot": {"statuses": [{"provider": "claude", "session_id": "old", "mode": "working"}]}},
                    expected])
    def snapshot():
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(harness, "snapshot", snapshot)
    monkeypatch.setattr(check.time, "sleep", lambda _: None)
    assert harness.wait_mode("claude", "new", "working") == expected


def test_missing_legacy_state_still_times_out(tmp_path, monkeypatch):
    harness = check.Harness(tmp_path / "result")
    ticks = iter([0, 0, 1, 11])
    monkeypatch.setattr(check.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(check.time, "sleep", lambda _: None)
    def missing():
        raise FileNotFoundError("never written")
    monkeypatch.setattr(harness, "snapshot", missing)
    with pytest.raises(AssertionError, match="did not reach"):
        harness.wait_mode("claude", "new", "working")


def test_invalid_legacy_state_is_not_hidden_as_startup_delay(tmp_path, monkeypatch):
    harness = check.Harness(tmp_path / "result")
    def invalid():
        raise ValueError("Invalid legacy live monitor state")
    monkeypatch.setattr(harness, "snapshot", invalid)
    with pytest.raises(ValueError, match="Invalid"):
        harness.wait_mode("claude", "new", "working")


def test_codex_package_preserves_helpers_and_resources(tmp_path):
    import io
    import tarfile
    archive = tmp_path / "package.tar.gz"
    files = {"bin/codex": b"cli", "bin/codex-code-mode-host": b"helper",
             "codex-package.json": b"{}", "codex-path/rg": b"rg"}
    with tarfile.open(archive, "w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    home = tmp_path / "home"
    package = check.install_codex_package(archive, home)
    assert (home / ".local/bin/codex").resolve() == package / "bin/codex"
    assert (home / ".local/bin/codex-code-mode-host").read_bytes() == b"helper"
    assert (package / "codex-path/rg").read_bytes() == b"rg"


def test_codex_bare_binary_archive_is_rejected(tmp_path):
    import tarfile
    archive = tmp_path / "bare.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.addfile(tarfile.TarInfo("bin/codex"))
    with pytest.raises(RuntimeError, match="Incomplete"):
        check.install_codex_package(archive, tmp_path / "home")


def test_duplicate_hooks_fail_and_external_hooks_are_not_executed(tmp_path):
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir()
    hooks = [{"hooks": [{"type": "command", "command": "other"},
                        {"type": "command", "command": "python sidepulse/hook_entry.py"}]}]
    path.write_text(json.dumps({"hooks": {"Stop": hooks}}))
    assert check.installed_commands("claude", tmp_path) == {"Stop": "python sidepulse/hook_entry.py"}
    path.write_text(json.dumps({"hooks": {"Stop": hooks * 2}}))
    with pytest.raises(AssertionError, match="Duplicate"):
        check.installed_commands("claude", tmp_path)


def test_fail_open_replay_is_not_a_pass(tmp_path, monkeypatch):
    from sidepulse import program_transport
    harness = check.Harness(tmp_path / "result")
    monkeypatch.setattr(check, "installed_commands", lambda *a: {e: "true" for e, _ in check.EVENTS})
    monkeypatch.setattr(harness, "wait_mode", lambda *a: {})
    monkeypatch.setattr(program_transport, "submit_report", lambda *a: {})
    with pytest.raises(AssertionError, match="did not log"):
        harness.replay("claude")


def test_codex_trust_metadata_is_not_treated_as_an_event(tmp_path):
    from sidepulse.install import install_codex_hooks
    config = tmp_path / ".codex/config.toml"
    install_codex_hooks(config_path=config, log_path=tmp_path / "codex.jsonl")
    before = check.installed_commands("codex", tmp_path)
    with config.open("a") as stream:
        stream.write('\n[hooks.state."/Users/test/.codex/config.toml:stop:0:0"]\ntrusted_hash = "sha256:test"\n')
    assert before
    assert check.installed_commands("codex", tmp_path) == before


def test_report_does_not_claim_live_success(tmp_path):
    harness = check.Harness(tmp_path / "result")
    harness.check("broken", lambda: 1 / 0)
    assert harness.save(False) == 1
    report = json.loads((harness.output / "report.json").read_text())
    assert report["live_agents"] == "not_run"
    assert report["failures"] == 1
    assert 'failure' in (harness.output / "junit.xml").read_text()


def test_bundle_excludes_secrets_git_and_symlinks(tmp_path):
    for name in host.FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test")
    (tmp_path / "src/pkg").mkdir(parents=True)
    (tmp_path / "src/pkg/main.py").write_text("pass")
    (tmp_path / "src/pkg/.env").write_text("SECRET")
    (tmp_path / "src/pkg/linked.py").symlink_to(tmp_path / "README.md")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config").write_text("private")
    bundle = host.prepare(tmp_path, tmp_path / "out")
    import tarfile
    with tarfile.open(bundle) as tar:
        names = tar.getnames()
    assert "src/pkg/main.py" in names
    assert "src/pkg/.env" not in names
    assert "src/pkg/linked.py" not in names
    assert ".git/config" not in names


def test_subprocess_timeout_is_bounded():
    with pytest.raises(RuntimeError, match="Timed out"):
        check.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=.05)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_real_installed_hook_replay_through_isolated_socket(tmp_path, monkeypatch, provider):
    from sidepulse.install import install_claude_hooks, install_codex_hooks
    from sidepulse.program_status import ProgramState
    from sidepulse.program_transport import ProgramServer
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SIDEPULSE_DISABLE_EVENT_SOCKET", "0")
    home = tmp_path / "guest"
    harness = check.Harness(tmp_path / "result")
    harness.home = home
    harness.state = tmp_path / "state/sidepulse/agent-monitor"
    config = home / (".claude/settings.json" if provider == "claude" else ".codex/config.toml")
    install = install_claude_hooks if provider == "claude" else install_codex_hooks
    install(log_path=harness.state / f"{provider}.jsonl", config_path=config)
    store = ProgramState(publisher="harness-test")
    server = ProgramServer(store)
    try:
        server.start()
        assert harness.replay(provider) == [e for e, _ in check.EVENTS]
        assert store.statuses() == ()
    finally:
        server.stop()
        store.close()


def test_evidence_receiver_rejects_invalid_requests_and_saves_exact_bytes(tmp_path, monkeypatch):
    import concurrent.futures
    import http.client
    import re
    import threading
    import time
    from http.server import HTTPServer

    collector = load("collect")
    servers = []
    ready = threading.Event()

    def server_factory(address, handler):
        server = HTTPServer(address, handler)
        servers.append(server)
        ready.set()
        return server

    monkeypatch.setattr(collector, "HTTPServer", server_factory)
    monkeypatch.setattr(collector, "MAX_BYTES", 1024)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        result = pool.submit(collector.receive, "127.0.0.1", tmp_path, 10)
        assert ready.wait(3)
        launcher = tmp_path / "collect-results.command"
        deadline = time.monotonic() + 3
        while not launcher.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        text = launcher.read_text()
        path = re.search(r'http://127\.0\.0\.1:\d+(/[^\s]+)', text).group(1)
        connection = http.client.HTTPConnection("127.0.0.1", servers[0].server_port)
        for target, body, expected in [("/wrong", b"data", 403), (path, b"x" * 1025, 413),
                                       (path, b"guest evidence bytes", 201)]:
            connection.request("POST", target, body)
            response = connection.getresponse()
            assert response.status == expected
            response.read()
        saved = result.result(timeout=3)
    assert saved.read_bytes() == b"guest evidence bytes"
    assert saved.with_suffix(".sha256").exists()
    assert not launcher.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_evidence_receiver_refuses_wildcard_interface(tmp_path):
    with pytest.raises(ValueError, match="specific private"):
        load("collect").receive("0.0.0.0", tmp_path)
