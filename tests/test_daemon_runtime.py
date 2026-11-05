"""Background behavior continues with no tray, and clients only observe it."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sidepulse.audit import append_status_history_record
from sidepulse.battery import BatterySnapshot
from sidepulse.device_writer import DeviceCandidate
from sidepulse.lid_sleep import MacSleepSnapshot
from sidepulse.program_presentation import DaemonMonitor
from sidepulse.program_transport import daemon_command, submit_report
from sidepulse.service import SidePulseService
from sidepulse.settings import (AgentMonitorSettings, SLEEP_PREVENTION_AGENTS,
    SLEEP_PREVENTION_NEVER, LID_ANIMATION_CLOSED)


@pytest.fixture
def background(tmp_path):
    from contextlib import ExitStack
    clock = SimpleNamespace(now=100.0)
    history = tmp_path / 'history.jsonl'
    candidate = DeviceCandidate(tmp_path, tmp_path/'LEDS.LED', 'dot')
    with ExitStack() as stack:
        def patched(name, **kwargs):
            return stack.enter_context(patch(name, **kwargs))
        patched('sidepulse.daemon_runtime.sleep_helper_installed', return_value=False)
        patched('sidepulse.daemon_runtime.external_display_is_active', return_value=False)
        patched('sidepulse.daemon_runtime.run_pmset_displaysleepnow')
        patched('sidepulse.daemon_runtime.time.monotonic', side_effect=lambda: clock.now)
        settings = patched('sidepulse.daemon_runtime.load_settings', return_value=AgentMonitorSettings(
            sleep_prevention_policy=SLEEP_PREVENTION_NEVER))
        patched('sidepulse.service.load_settings', side_effect=lambda: settings.return_value)
        battery = patched('sidepulse.daemon_runtime.read_battery_snapshot', return_value=BatterySnapshot(percent=80))
        lid = patched('sidepulse.daemon_runtime.read_lid_closed', return_value=False)
        sleep = patched('sidepulse.daemon_runtime.read_mac_sleep_snapshot', return_value=MacSleepSnapshot())
        devices = patched('sidepulse.daemon_runtime.discover_devices', return_value=[candidate])
        patched('sidepulse.service.discover_devices', return_value=[candidate])
        writes = patched('sidepulse.service.write_led_program', return_value=candidate.target)
        patched('sidepulse.daemon_runtime.append_status_history_record',
            side_effect=lambda record: append_status_history_record(record, path=history))
        service = SidePulseService()
        service.runtime.is_mac = True
        process = Mock()
        process.poll.return_value = None
        service.runtime.keep_awake.process_factory = Mock(return_value=process)
        service.runtime.keep_awake.poke_status_file = Mock(return_value=None)
        with tempfile.TemporaryDirectory(prefix='sp-runtime-', dir='/tmp') as directory:
            service.program_server.path = Path(directory)/'status.sock'
            service.program_server.start()
            def tick(at):
                clock.now = at
                service.runtime.poll()
                service.sync_outputs()
            try:
                yield SimpleNamespace(service=service, runtime=service.runtime, clock=clock,
                    settings=settings, battery=battery, lid=lid, sleep=sleep, devices=devices,
                    writes=writes, tick=tick, history=history, path=service.program_server.path)
            finally:
                service.program_server.stop()
                service.runtime.close()


def report(state):
    return {'v':1, 'type':'program_status', 'scope':'session', 'status':{'state':state}}




def test_headless_daemon_records_history_and_drives_outputs_through_ui_reconnect(background):
    b=background
    submit_report(report('working'), path=b.path)
    b.tick(100)
    first=DaemonMonitor(b.path)
    first.snapshot()
    assert first.runtime['battery']['percent'] == 80
    assert b.writes.call_count == 1
    # Tray refresh and reconnect do not run policy, write LEDs, or record samples.
    for _ in range(3): DaemonMonitor(b.path).snapshot()
    assert b.writes.call_count == 1
    assert len(b.history.read_text().splitlines()) == 1
    del first
    submit_report(report('blocked'), path=b.path)
    b.tick(115)
    rows=[json.loads(line) for line in b.history.read_text().splitlines()]
    assert [r['agent_status'] for r in rows] == ['working','waiting_for_input']
    assert [r['display_status'] for r in rows] == ['Working','Ask']
    assert b.writes.call_count == 2


def test_daemon_owns_sleep_grace_and_low_battery_safeguard(background):
    b=background
    b.settings.return_value=AgentMonitorSettings(sleep_prevention_policy=SLEEP_PREVENTION_AGENTS)
    submit_report(report('working'), path=b.path)
    b.tick(100)
    assert b.runtime.keep_awake.process_running()
    b.battery.return_value=BatterySnapshot(percent=10)
    b.tick(101)
    assert not b.runtime.keep_awake.process_running()
    assert b.runtime.battery_sleep_safeguard_active
    b.battery.return_value=BatterySnapshot(percent=10,is_plugged=True)
    b.tick(102)
    assert b.runtime.keep_awake.process_running()
    submit_report(report('done'), path=b.path)
    b.tick(103)
    assert b.runtime.keep_awake.process_running()
    b.tick(402)
    assert b.runtime.keep_awake.process_running()
    b.tick(403)
    assert not b.runtime.keep_awake.process_running()


def test_daemon_lid_animation_hold_and_restore_require_no_tray(background):
    b=background
    submit_report(report('working'),path=b.path)
    b.tick(100)
    b.lid.return_value=True
    b.tick(101)
    assert b.runtime.lid_closed_led_hold_active
    assert b.writes.call_count == 2  # Normal status, then close animation.
    b.tick(120)
    assert b.writes.call_count == 2  # Final closed frame remains held.
    b.lid.return_value=False
    b.tick(121)
    assert not b.runtime.lid_closed_led_hold_active
    assert b.writes.call_count == 3
    b.tick(140)
    assert b.writes.call_count == 4  # Daemon restores normal status after open animation.


def test_manual_preview_expiry_and_firmware_pause_survive_tray_disconnect(background):
    b=background
    submit_report(report('working'),path=b.path)
    b.tick(100)
    daemon_command({'type':'write_led','target':str(b.writes.return_value),
        'program':'#FF0000','hold_seconds':10},path=b.path)
    b.tick(105)
    assert b.writes.call_count == 2
    daemon_command({'type':'output_control','pause_seconds':20},path=b.path)
    # An unrelated command must not reset a firmware pause or manual hold.
    daemon_command({'type':'output_control','enabled':True},path=b.path)
    b.tick(115)
    assert b.writes.call_count == 2
    assert len(b.history.read_text().splitlines()) == 2
    b.tick(126)
    assert b.writes.call_count == 3


def test_ui_disconnect_and_connect_are_executed_by_daemon(background):
    b=background
    submit_report(report('working'),path=b.path)
    b.tick(100)
    daemon_command({'type':'output_control','enabled':False},path=b.path)
    assert b.writes.call_count == 2
    b.tick(115)
    assert b.writes.call_count == 2
    assert len(b.history.read_text().splitlines()) == 2
    daemon_command({'type':'output_control','enabled':True},path=b.path)
    b.tick(116)
    assert b.writes.call_count == 3
    assert len(b.service.program_state.statuses()) == 1


def test_lid_preview_command_supports_unsaved_program_and_daemon_timing(background):
    b=background
    b.tick(100)
    daemon_command({'type':'play_lid_animation','kind':LID_ANIMATION_CLOSED,
        'program':'#FF0000','duration_seconds':5},path=b.path)
    assert '#FF0000' in b.writes.call_args.args[0]
    assert b.service.animation_until == 105.15
    b.tick(104)
    assert b.writes.call_count == 2
    b.tick(106)
    assert b.writes.call_count == 3


def test_poll_errors_back_off_without_stopping_history_or_leds(background):
    b=background
    b.lid.side_effect=OSError('lid unavailable')
    b.sleep.return_value=MacSleepSnapshot(error='sleep unavailable')
    b.tick(100)
    b.tick(115)
    assert b.lid.call_count == b.sleep.call_count == 1
    assert b.runtime.view['lid_error'] == 'lid unavailable'
    assert len(b.history.read_text().splitlines()) == 2
    b.tick(131)
    assert b.lid.call_count == b.sleep.call_count == 2


def test_daemon_shutdown_releases_sleep_controls_even_after_failure(background):
    b=background
    with patch.object(b.runtime.closed_lid_awake,'release',side_effect=RuntimeError('helper failed')) as closed, patch.object(b.runtime.keep_awake,'release') as awake:
        with pytest.raises(RuntimeError,match='helper failed'):
            b.runtime.close()
    closed.assert_called_once()
    awake.assert_called_once()


def test_slow_power_observation_does_not_block_hooks_or_state_queries(background):
    import threading
    b=background
    entered=threading.Event()
    release=threading.Event()
    def slow_read(**kwargs):
        entered.set()
        assert release.wait(3)
        return BatterySnapshot(percent=75)
    b.battery.side_effect=slow_read
    worker=threading.Thread(target=b.runtime.poll)
    worker.start()
    try:
        assert entered.wait(1)
        assert submit_report(report('working'),path=b.path,timeout=.2)['ok']
        assert DaemonMonitor(b.path).snapshot().aggregate.active_count == 1
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()


def test_history_failure_is_reported_and_sampling_recovers(background):
    b=background
    with patch('sidepulse.daemon_runtime.append_status_history_record',side_effect=OSError('disk full')):
        b.tick(100)
    view=DaemonMonitor(b.path)
    view.snapshot()
    assert view.runtime['history_error'] == 'disk full'
    b.tick(115)
    assert b.runtime.view['history_error'] is None
    assert len(b.history.read_text().splitlines()) == 1


def test_non_mac_daemon_skips_mac_power_controls(background):
    b=background
    b.runtime.is_mac=False
    b.tick(100)
    b.battery.assert_not_called()
    b.lid.assert_not_called()
    b.sleep.assert_not_called()
    assert len(b.history.read_text().splitlines()) == 1
    assert b.writes.call_count == 1


def test_sleep_switch_is_applied_by_runtime_not_socket_threads(background):
    b=background
    b.settings.return_value=AgentMonitorSettings(sleep_prevention_policy=SLEEP_PREVENTION_AGENTS)
    submit_report(report('working'),path=b.path)
    b.tick(100)
    assert b.runtime.keep_awake.process_running()
    with patch.object(b.runtime.keep_awake,'set_enabled',wraps=b.runtime.keep_awake.set_enabled) as enabled:
        daemon_command({'type':'output_control','keep_awake_enabled':False},path=b.path)
        enabled.assert_not_called()
        view=DaemonMonitor(b.path)
        view.snapshot()
        assert view.runtime['keep_awake_enabled'] is False
        b.tick(101)
        enabled.assert_called_once_with(False)
    assert not b.runtime.keep_awake.process_running()
    assert not b.runtime.closed_lid_awake.active()
    daemon_command({'type':'output_control','keep_awake_enabled':True},path=b.path)
    b.tick(102)
    assert b.runtime.keep_awake.process_running()
