from unittest.mock import Mock

import pytest

from sidepulse.internal_display import InternalDisplayController, builtin_framebuffer_id


def test_selects_only_internal_framebuffer():
    entries = [
        {"IONameMatched": "dispext0,t604x", "IORegistryEntryID": 1},
        {"IONameMatched": "disp0,t604x", "IORegistryEntryID": 2},
        {"IONameMatched": "dispext1,t604x", "IORegistryEntryID": 3},
    ]
    assert builtin_framebuffer_id(entries) == 2
    with pytest.raises(RuntimeError):
        builtin_framebuffer_id(entries[:1])
    with pytest.raises(RuntimeError):
        builtin_framebuffer_id([entries[1], entries[1]])


def test_lid_cycle_and_repeated_polls(tmp_path):
    power = Mock()
    panel = InternalDisplayController(power, recovery_path=tmp_path / "recovery")
    panel.update(False)
    panel.update(None)
    power.assert_not_called()
    panel.update(True)
    panel.update(True)
    power.assert_called_once_with(False)
    panel.update(False)
    panel.release()
    assert [call.args for call in power.call_args_list] == [(False,), (True,)]
    assert not panel.recovery_path.exists()


def test_retries_failed_off_and_restores_after_uncertain_result(tmp_path):
    power = Mock(side_effect=[RuntimeError("failed"), None, RuntimeError("wake"), None])
    panel = InternalDisplayController(power, recovery_path=tmp_path / "recovery")
    panel.update(True)
    assert panel.last_error == "failed"
    assert panel.recovery_path.exists()
    panel.update(True)
    assert panel.last_error is None
    panel.update(False)
    assert panel.powered_off
    panel.update(False)
    assert not panel.powered_off
    assert [call.args for call in power.call_args_list] == [(False,), (False,), (True,), (True,)]


@pytest.mark.parametrize("closed", [False, True])
def test_restart_recovers_recorded_panel_state(tmp_path, closed):
    path = tmp_path / "recovery"
    path.touch()
    power = Mock()
    panel = InternalDisplayController(power, recovery_path=path)
    panel.update(closed)
    power.assert_called_once_with(not closed)
    panel.release()
    assert not path.exists()


def test_open_after_failed_off_still_restores(tmp_path):
    power = Mock(side_effect=[RuntimeError("uncertain"), None])
    panel = InternalDisplayController(power, recovery_path=tmp_path / "recovery")
    panel.update(True)
    panel.update(False)
    assert [call.args for call in power.call_args_list] == [(False,), (True,)]
    assert not panel.powered_off


def test_failed_second_close_retries(tmp_path):
    power = Mock(side_effect=[None, None, RuntimeError("off"), None])
    panel = InternalDisplayController(power, recovery_path=tmp_path / "recovery")
    for closed in [True, False, True, True]:
        panel.update(closed)
    assert power.call_count == 4
    assert panel.last_error is None


def test_backend_initialization_error_is_cached():
    from sidepulse.internal_display import BuiltinPanelPower

    backend = BuiltinPanelPower()
    backend._connect = Mock(side_effect=RuntimeError("unsupported"))
    with pytest.raises(RuntimeError, match="unsupported"):
        backend(False)
    with pytest.raises(RuntimeError, match="unsupported"):
        backend(False)
    backend._connect.assert_called_once()
