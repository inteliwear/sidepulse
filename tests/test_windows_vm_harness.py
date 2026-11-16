"""Safety checks for the Windows harness; these are not Windows product coverage."""
import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('windows_vm_' + name,
        ROOT / 'scripts/windows-vm' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failed_fresh_precondition_cannot_run_setup_on_existing_environment(tmp_path, monkeypatch):
    module = load('probe')
    probe = module.Probe(tmp_path, 'fresh')
    probe.python.parent.mkdir(parents=True)
    probe.python.touch()
    monkeypatch.setattr(probe, 'command', lambda *a, **k: pytest.fail('must not run product commands'))
    result = probe.run()
    assert result['failures'] == 1
    assert [r['name'] for r in result['checks']] == ['install']
    assert probe.python.exists()


def test_failed_installer_cannot_pass_using_previous_installation(tmp_path, monkeypatch):
    module = load('probe')
    probe = module.Probe(tmp_path, 'repeat')
    probe.python.parent.mkdir(parents=True)
    probe.python.touch()
    calls = []

    def fail(name, *args, **kwargs):
        calls.append(name)
        raise RuntimeError('installer failed')

    monkeypatch.setattr(probe, 'command', fail)
    result = probe.run()
    assert calls == ['pip-install']
    assert result['failures'] == 1
    assert result['real_agents_run'] is False
    assert result['visual_comparison_run'] is False


def test_test_only_rerun_requires_matching_product_and_never_reinstalls(tmp_path, monkeypatch):
    module = load('probe')
    probe = module.Probe(tmp_path, 'unit')
    monkeypatch.setattr(probe, 'install', lambda: pytest.fail('test-only rerun must not reinstall'))
    monkeypatch.setattr(probe, 'full_suite', lambda: pytest.fail('mismatched product must not pass tests'))
    def mismatch():
        raise RuntimeError('installed product differs from frozen source')
    monkeypatch.setattr(probe, 'source_match', mismatch)
    result = probe.run()
    assert result['failures'] == 1
    assert [c['name'] for c in result['checks']] == ['installed-source']


def test_unverified_installer_cannot_produce_bootstrap_media(tmp_path):
    module = load('media')
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    (downloads / 'python-3.13.17-arm64.exe').write_bytes(b'not the vendor installer')
    destination = tmp_path / 'frozen'
    with pytest.raises(ValueError, match='checksum'):
        module.build(destination, downloads)
    assert not destination.exists()


def test_frozen_media_cannot_be_replaced(tmp_path):
    module = load('media')
    destination = tmp_path / 'existing'
    destination.mkdir()
    sentinel = destination / 'evidence'
    sentinel.write_text('retain')
    with pytest.raises(ValueError, match='never overwritten'):
        module.build(destination, tmp_path)
    assert sentinel.read_text() == 'retain'


def test_running_windows_vm_cannot_be_cloned(tmp_path, monkeypatch):
    module = load('host')
    monkeypatch.setattr(module, 'load', lambda _: {'status': 'started'})
    monkeypatch.setattr(module, 'call', lambda *a: pytest.fail('must not clone a live disk'))
    destination = tmp_path / 'clone'
    with pytest.raises(ValueError, match='Shut down'):
        module.clone(tmp_path / 'source', destination, 'SidePulse Windows Fresh')
    assert not destination.exists()


def test_host_registration_cannot_claim_an_unrelated_vm(tmp_path, monkeypatch):
    module = load('host')
    identifier = 'A' * 36
    monkeypatch.setattr(module, 'inventory', lambda: {identifier: {
        'uuid': identifier, 'status': 'stopped', 'name': 'Personal macOS'}})
    destination = tmp_path / 'registered'
    with pytest.raises(ValueError, match='Windows test VMs'):
        module.register(destination, identifier)
    assert not destination.exists()


def test_utm_scripting_error_with_zero_exit_is_not_success(monkeypatch):
    module = load('host')
    result = module.subprocess.CompletedProcess([], 0, '',
        'Error from event: failed to open file: The system cannot find the path specified.')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: result)
    with pytest.raises(RuntimeError, match='cannot find the path'):
        module.call('file', 'pull', 'test-vm', 'missing-file')


@pytest.mark.parametrize('root', ['..', '.codex', r'SidePulseFresh\..\..', 'SidePulseFresh/../Secrets'])
def test_evidence_collection_cannot_read_an_arbitrary_profile_path(tmp_path, root, monkeypatch):
    module = load('host')
    monkeypatch.setattr(module, 'load', lambda *a: pytest.fail('invalid path must not reach the VM'))
    with pytest.raises(ValueError, match='without paths'):
        module.collect(tmp_path, root, tmp_path / 'output')


def test_collection_cannot_replace_previous_evidence(tmp_path, monkeypatch):
    module = load('host')
    output = tmp_path / 'prior-results'
    output.mkdir()
    monkeypatch.setattr(module, 'load', lambda *a: pytest.fail('must retain existing evidence'))
    with pytest.raises(ValueError, match='retained'):
        module.collect(tmp_path, 'SidePulseFresh', output)


def test_collector_extracts_windows_archive_paths(tmp_path):
    module = load('host')
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr(r'evidence\run\result.json', '{"ok":true}')
    module.extract_evidence(data.getvalue(), tmp_path / 'collected')
    assert (tmp_path / 'collected/evidence/run/result.json').read_text() == '{"ok":true}'


@pytest.mark.parametrize('name', [r'evidence\..\..\outside', r'C:\Users\test\.codex\auth.json'])
def test_collector_rejects_windows_traversal_before_creating_output(tmp_path, name):
    module = load('host')
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr(name, 'private')
    with pytest.raises(RuntimeError, match='Unsafe'):
        module.extract_evidence(data.getvalue(), tmp_path / 'collected')
    assert not (tmp_path / 'collected').exists()


def test_unresponsive_guest_is_retained_without_force_shutdown(tmp_path, monkeypatch):
    module = load('host')
    calls = []
    monkeypatch.setattr(module, 'load', lambda *a: {'uuid': 'test-vm', 'status': 'started'})
    monkeypatch.setattr(module, 'call', lambda *a: calls.append(a))
    clock = iter([0, 0, 61])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='retained'):
        module.stop(tmp_path)
    assert calls == [('exec', 'test-vm', '--cmd', r'C:\Windows\System32\shutdown.exe', '/s', '/t', '0')]


def test_missing_runs_and_unrun_agents_cannot_be_reported_as_passed(tmp_path):
    module = load('report')
    result = module.summarize(tmp_path)
    assert result['status'] == 'incomplete'
    assert all(r['status'] == 'not_run' for r in result['scenarios'])
    assert result['real_agents'] == result['agent_screen_comparison'] == 'not_run'


def test_failed_unit_rerun_keeps_report_failed_when_install_checks_pass(tmp_path):
    module = load('report')
    for _, machine, stage in module.SCENARIOS:
        directory = tmp_path / machine / stage
        directory.mkdir(parents=True)
        (directory / 'result.json').write_text(json.dumps({
            'scope': 'native-windows', 'stage': stage,
            'checks': [{'name': 'install', 'status': 'passed'}]}))
    rerun = tmp_path / 'unit'
    rerun.mkdir()
    (rerun / 'result.json').write_text(json.dumps({
        'scope': 'native-windows', 'stage': 'unit', 'failures': 1,
        'checks': [{'name': 'full-python-suite', 'status': 'failed'}]}))
    (rerun / 'full-suite.log').write_text('164 failed, 714 passed, 90 skipped, 77 errors, 422 subtests passed')
    result = module.summarize(tmp_path)
    assert result['status'] == 'failed'
    assert result['unit_runs'][0]['counts'] == {
        'failed': 164, 'passed': 714, 'skipped': 90, 'errors': 77, 'subtests_passed': 422}


def test_baseline_rejects_prior_probe_install_even_when_base_python_is_clean(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/windows-vm'))
    module = load('baseline')
    home = tmp_path / 'test'
    (home / 'SidePulseHarness').mkdir(parents=True)
    (home / 'SidePulseHarness/vm.json').write_text(json.dumps({'prepared_sources': []}))
    (home / 'SidePulseFresh/venv/Lib/site-packages/sidepulse-0.dist-info').mkdir(parents=True)
    monkeypatch.setattr(module, 'guard', lambda _: None)
    monkeypatch.setattr(module.Path, 'home', lambda: home)
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **kw: pytest.fail('must reject prior install first'))
    with pytest.raises(RuntimeError, match='prior SidePulse probe'):
        module.main()


def test_windows_screen_review_cannot_pass_without_real_tool_and_fixture_proof(tmp_path):
    module = load('agents')
    (tmp_path / 'recording.json').write_text(json.dumps({
        'run_id': 'test', 'provider': 'codex', 'version': 'test', 'error': None,
        'exit_code': 0, 'frames': [], 'live_checks': {}}))
    result = module.review(tmp_path, {'run_id': 'test', 'screenshots': []})
    assert result['status'] == 'failed'
    assert set(result['missing_states']) == {'working', 'waiting_for_input', 'completed'}


def test_windows_screen_review_rejects_other_recordings_and_traversal(tmp_path):
    module = load('agents')
    (tmp_path / 'recording.json').write_text(json.dumps({
        'run_id': 'this-run', 'provider': 'codex', 'frames': []}))
    with pytest.raises(ValueError, match='another run'):
        module.review(tmp_path, {'run_id': 'another-run'})
    with pytest.raises(ValueError, match='inside this recording'):
        module.review(tmp_path, {'run_id': 'this-run', 'screenshots': [{
            'expected': 'working', 'image': '../outside.png'}]})


def test_windows_screen_review_keeps_independent_visual_defects_failed(tmp_path):
    module = load('agents')
    (tmp_path / 'recording.json').write_text(json.dumps({
        'run_id': 'test', 'provider': 'claude', 'version': 'test',
        'error': None, 'exit_code': 0, 'frames': [],
        'live_checks': {'actual_fixture': True}}))
    result = module.review(tmp_path, {'run_id': 'test', 'screenshots': [],
        'observed_defect': 'Terminal input was corrupted.'})
    assert result['status'] == 'failed'
    assert result['observed_defect'] == 'Terminal input was corrupted.'


def test_runtime_report_requires_complete_installer_suite_reboot_and_live_evidence(tmp_path):
    module = load('runtime_report')
    assert module.summarize(tmp_path)['status'] == 'incomplete'
    result = tmp_path / 'result.json'
    result.write_text(json.dumps({'scope': 'native-windows', 'install_method': 'native-powershell-installer',
        'checks': [{'name': name, 'status': 'passed'} for name in module.REQUIRED - {'full-python-suite'}]}))
    (tmp_path / 'runs.json').write_text(json.dumps({'fresh-arm64': 'result.json'}))
    data = module.summarize(tmp_path)
    assert data['status'] == data['installations'][0]['status'] == 'failed'
    assert all(row['status'] == 'not_run' for row in data['reboots'] + data['real_agents'])


def test_runtime_report_rejects_paths_outside_reviewed_evidence(tmp_path):
    module = load('runtime_report')
    (tmp_path / 'runs.json').write_text('{"fresh-arm64":"../private.json"}')
    with pytest.raises(ValueError, match='inside'):
        module.summarize(tmp_path)
