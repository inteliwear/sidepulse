"""Reject unsafe VM operations and false installation passes."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('linux_vm_' + name, ROOT / 'scripts/linux-vm' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load('host')
guest = load('guest')
terminal = load('terminal')
report = load('report')


def test_clone_rejects_running_vm_before_creating_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(host, 'load', lambda _: {})
    monkeypatch.setattr(host, 'running', lambda _: True)
    destination = tmp_path / 'clone'
    with pytest.raises(ValueError, match='Shut down'):
        host.clone(tmp_path / 'source', destination, 22281)
    assert not destination.exists()


def test_clone_does_not_share_mutable_baseline_layer(tmp_path, monkeypatch):
    source, destination = tmp_path / 'source', tmp_path / 'clone'
    source.mkdir()
    (source / 'vm.json').write_text(json.dumps({'schema': 1, 'kind': 'sidepulse-disposable-linux'}))
    for name in ('disk.qcow2', 'vars.fd', 'seed.iso', 'id_ed25519', 'id_ed25519.pub'):
        (source / name).write_text('baseline')
    monkeypatch.setattr(host, 'running', lambda _: False)
    host.clone(source, destination, 22281)
    (source / 'disk.qcow2').write_text('later changes')
    assert (destination / 'disk.qcow2').read_text() == 'baseline'
    assert host.load(destination)['ssh_port'] == 22281


def test_unresponsive_live_qemu_cannot_be_treated_as_stopped(tmp_path, monkeypatch):
    (tmp_path / 'qemu.pid').write_text('123')
    monkeypatch.setattr(host, 'qmp', lambda *a: (_ for _ in ()).throw(OSError('no socket')))
    monkeypatch.setattr(host.os, 'kill', lambda *a: None)
    with pytest.raises(RuntimeError, match='alive'):
        host.running(tmp_path)


def test_image_checksum_mismatch_is_not_booted_or_removed(tmp_path):
    (tmp_path / 'SHA256SUMS').write_text('0' * 64 + ' *' + host.IMAGE_NAME + '\n')
    image = tmp_path / host.IMAGE_NAME
    image.write_bytes(b'corrupted cloud image')
    with pytest.raises(ValueError, match='SHA256SUMS'):
        host.download_image(tmp_path)
    assert image.read_bytes() == b'corrupted cloud image'


def test_fresh_refuses_existing_install_without_invoking_setup(tmp_path, monkeypatch):
    python = tmp_path / 'python'
    python.touch()
    monkeypatch.setattr(guest, 'PYTHON', python)
    monkeypatch.setattr(guest, 'RESULTS', tmp_path / 'results')
    monkeypatch.setattr(guest.subprocess, 'run', lambda *a, **k: pytest.fail('must not install'))
    with pytest.raises(AssertionError, match='clean baseline'):
        guest.install('fresh')


def test_installed_source_mismatch_fails_even_when_version_matches(tmp_path, monkeypatch):
    package = tmp_path / 'installed/sidepulse'
    package.mkdir(parents=True)
    (package / 'collector.py').write_text('wrong code')
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'source-manifest.json').write_text(json.dumps({'src/sidepulse/collector.py': '0' * 64}))
    monkeypatch.setattr(guest, 'SOURCE', source)
    monkeypatch.setitem(sys.modules, 'sidepulse', SimpleNamespace(__file__=str(package / '__init__.py')))
    harness = guest.LinuxHarness(tmp_path / 'result', 'fresh')
    with pytest.raises(AssertionError, match='Installed source differs'):
        harness.installed_source()


def test_linux_report_cannot_claim_live_or_visual_pass_from_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(guest, 'RESULTS', tmp_path)
    harness = guest.LinuxHarness(tmp_path / 'result', 'fresh')
    harness.results = [{'name': 'example failure', 'status': 'failed', 'detail': 'missing'}]
    # Report generation itself is host-portable for regression tests.
    path_type = guest.Path
    real_read = path_type.read_text
    def read(path, *args, **kwargs):
        if path == path_type('/etc/os-release'):
            return 'ID=ubuntu\n'
        if path == guest.Path('/proc/sys/kernel/random/boot_id'):
            return 'test-boot-id\n'
        return real_read(path, *args, **kwargs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(path_type, 'read_text', read)
        assert harness.save(False) == 1
    report = json.loads((harness.output / 'report.json').read_text())
    assert report['live_agents'] == report['visual_verification'] == 'not_run'
    assert report['failures'] == 1


def test_new_pid_does_not_pass_recovery_until_daemon_responds(tmp_path, monkeypatch):
    from sidepulse.program_status import StatusError
    harness = guest.LinuxHarness(tmp_path / 'result', 'verify')
    pids = iter(['100', '200', '200'])
    monkeypatch.setattr(guest, 'capture', lambda *a: next(pids))
    monkeypatch.setattr(guest.subprocess, 'run', lambda *a, **k: None)
    monkeypatch.setattr(guest.time, 'sleep', lambda *a: None)
    replies = iter([StatusError('starting'), {'ok': True}])
    def snapshot():
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(harness, 'snapshot', snapshot)
    assert harness.recovery() == {'old_pid': '100', 'new_pid': '200'}


def test_reboot_cannot_pass_with_same_boot_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(guest, 'RESULTS', tmp_path)
    (tmp_path / 'last-observed-boot.json').write_text('{"boot_id":"same-boot"}')
    harness = guest.LinuxHarness(tmp_path / 'result', 'reboot')
    read_text = guest.Path.read_text
    monkeypatch.setattr(guest.Path, 'read_text', lambda path, *a, **k:
                        'same-boot' if path == guest.Path('/proc/sys/kernel/random/boot_id') else read_text(path, *a, **k))
    with pytest.raises(AssertionError, match='No real OS reboot'):
        harness.reboot()


def test_terminal_capture_requires_independent_labels_and_three_states(tmp_path):
    data = {'run_id': 'test', 'frames': []}
    assert terminal.review(data, {'run_id': 'test', 'labels': {}}, tmp_path)['status'] == 'incomplete'
    with pytest.raises(ValueError, match='another recording'):
        terminal.review(data, {'run_id': 'other'}, tmp_path)


def test_changed_terminal_cells_cannot_satisfy_visual_review(tmp_path):
    import hashlib
    screen = tmp_path / 'screen.txt'
    screen.write_text('original cells')
    checksum = hashlib.sha256(screen.read_bytes()).hexdigest()
    data = {'run_id': 'test', 'frames': [
        {'id': index, 'screen': screen.name, 'screen_sha256': checksum, 'detected': [state]}
        for index, state in enumerate(['working', 'waiting_for_input', 'completed'])]}
    labels = {'run_id': 'test', 'labels': {'0': 'working', '1': 'waiting_for_input', '2': 'completed'}}
    assert terminal.review(data, labels, tmp_path)['status'] == 'passed'
    screen.write_text('edited after capture')
    assert terminal.review(data, labels, tmp_path)['status'] == 'failed'


def test_captured_terminal_wait_cannot_pass_if_detector_says_completed(tmp_path):
    import hashlib
    path = tmp_path / 'screen.txt'
    path.write_text('Implement this plan?')
    data = {'run_id': 'test', 'frames': [{'id': 0, 'screen': path.name,
            'screen_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'detected': ['completed']}]}
    result = terminal.review(data, {'run_id': 'test', 'labels': {'0': 'waiting_for_input'}}, tmp_path)
    assert result['status'] == 'failed' and not result['comparisons'][0]['matches']


def test_report_cannot_promote_missing_vm_or_live_evidence_to_a_pass(tmp_path):
    result = report.summarize(tmp_path)
    assert result['installation_status'] != 'passed'
    assert result['live_agents'] == result['terminal_review'] == 'not_run'


def test_report_requires_both_actual_agents_not_only_successful_replays(tmp_path):
    path = tmp_path / 'fresh/live'
    path.mkdir(parents=True)
    (path / 'report.json').write_text(json.dumps({'stage': 'live', 'live_agents': 'attempted',
        'results': [{'name': 'installed-hooks-codex', 'status': 'passed'},
                    {'name': 'real-agent-claude', 'status': 'passed'}]}))
    assert report.summarize(tmp_path)['live_agents'] == 'partial'


@pytest.mark.parametrize('previous', [True, False])
def test_device_check_restores_previous_output_control_even_if_watcher_fails(tmp_path, monkeypatch, previous):
    import sidepulse.device_events as events
    import sidepulse.program_transport as transport
    calls = []
    watcher = SimpleNamespace(start=lambda: (_ for _ in ()).throw(OSError('watcher failed')),
                              close=lambda: None)
    harness = guest.LinuxHarness(tmp_path / 'result', 'verify')
    monkeypatch.setattr(harness, 'snapshot', lambda: {'outputs_enabled': previous})
    monkeypatch.setattr(events, 'DeviceEvents', lambda: watcher)
    monkeypatch.setattr(transport, 'daemon_command', lambda command: calls.append(command))
    monkeypatch.setattr(guest.subprocess, 'run', lambda *a, **k: None)
    monkeypatch.setattr(guest.os.path, 'ismount', lambda _: False)
    with pytest.raises(OSError, match='watcher failed'):
        harness.device()
    assert calls == [{'type': 'output_control', 'enabled': False},
                     {'type': 'output_control', 'enabled': previous}]


@pytest.mark.parametrize('existing', [True, False])
def test_agent_prerequisites_preserve_existing_profile_and_global_restriction(tmp_path, monkeypatch, existing):
    paths = {name: tmp_path / name for name in ['profile', 'supplied', 'restriction']}
    paths['supplied'].write_text('distro profile')
    paths['restriction'].write_text('1\n')
    if existing:
        paths['profile'].write_text('existing policy')
    mapping = {'/etc/apparmor.d/bwrap-userns-restrict': paths['profile'],
               '/usr/share/apparmor/extra-profiles/bwrap-userns-restrict': paths['supplied'],
               '/proc/sys/kernel/apparmor_restrict_unprivileged_userns': paths['restriction']}
    path_type = guest.Path
    monkeypatch.setattr(guest, 'Path', lambda value: mapping.get(str(value), path_type(value)))
    monkeypatch.setattr(guest, 'capture', lambda *a: 'bubblewrap 0.9.0')
    calls, probes = [], []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ['sudo', 'install']:
            paths['profile'].write_bytes(paths['supplied'].read_bytes())
        if argv[0] == 'bwrap':
            probes.append(argv)
            return SimpleNamespace(returncode=1 if len(probes) == 1 else 0)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(guest.subprocess, 'run', run)
    result = guest.agent_prerequisites(tmp_path)
    assert result['global_userns_restriction'] == '1'
    assert paths['restriction'].read_text() == '1\n'
    assert paths['profile'].read_text() == ('existing policy' if existing else 'distro profile')
    assert not any('sysctl' in argv for argv in calls)
