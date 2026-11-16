"""Real native filesystem events, without touching physical device files."""
from pathlib import Path
import sys
import threading
from unittest.mock import Mock

import pytest

from sidepulse import device_events, device_writer


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    root = tmp_path / 'mounts'
    root.mkdir()
    monkeypatch.setenv('SIDEPULSE_MOUNT_ROOTS', str(root))
    monkeypatch.setattr(device_events, 'default_mount_roots', lambda: (root,))
    monkeypatch.setattr(device_events, 'native_events', lambda changed: Mock())
    events = device_events.DeviceEvents()
    events.start()
    events.changed.clear()
    try:
        yield events, root
    finally:
        events.close()
    assert not events.observer.is_alive()
    assert not events.observer.emitters


def test_mount_creation_removal_and_rename_invalidate_inventory(watcher):
    events, root = watcher
    dot = root / 'SidePulseDot'
    dot.mkdir()
    assert events.changed.wait(5)
    events.update()
    events.changed.clear()
    renamed = dot.rename(root / 'PulseDot')
    assert events.changed.wait(5)
    events.changed.clear()
    renamed.rmdir()
    assert events.changed.wait(5)


def test_reads_led_writes_and_unrelated_files_do_not_trigger_scans(watcher):
    events, root = watcher
    dot = root / 'SidePulseDot'
    dot.mkdir()
    assert events.changed.wait(5)
    events.changed.clear()
    for _ in range(10):
        (dot / 'LEDS.LED').write_text('off')
        (dot / 'STATUS.TXT').write_text('serial=TEST')
        (dot / 'STATUS.TXT').read_text()
        list(root.iterdir())
        (root / 'unrelated.txt').write_text('ignored')
    assert not events.changed.wait(1.5)


def test_missing_mount_folder_is_watched_when_it_appears(watcher, monkeypatch):
    events, root = watcher
    missing = root / 'new-user' / 'volumes'
    monkeypatch.setattr(device_events, 'default_mount_roots', lambda: (missing,))
    events.update()
    events.changed.clear()
    (root / 'unrelated').mkdir()
    assert not events.changed.wait(1.5)
    missing.mkdir(parents=True)
    assert events.changed.wait(5)
    events.update()
    events.changed.clear()
    (missing / 'SidePulseDot').mkdir()
    assert events.changed.wait(5)


def test_recreated_mount_root_is_watched_again(watcher):
    events, root = watcher
    root.rmdir()
    assert events.changed.wait(5)
    events.update()
    events.changed.clear()
    root.mkdir()
    assert events.changed.wait(5)
    events.update()
    events.changed.clear()
    (root / 'SidePulseDot').mkdir()
    assert events.changed.wait(5)


def test_native_listener_starts_and_stops_without_polling():
    changed = threading.Event()
    native = device_events.native_events(changed)
    try:
        native.start()
        assert native.thread.is_alive()
        assert getattr(native, 'error', None) is None
        if sys.platform == 'win32':
            native.user.PostMessageW(native.window, 0x0219, 0x0007, 0)
            assert changed.wait(5)
    finally:
        native.close()
    assert native.thread is None or not native.thread.is_alive()


def test_windows_removed_directory_reported_as_file_still_invalidates_inventory(tmp_path, monkeypatch):
    from watchdog.events import FileDeletedEvent, DirMovedEvent
    root = tmp_path / 'mounts'
    root.mkdir()
    dot = root / 'SidePulseDot'
    dot.mkdir()
    unrelated = root / 'notes.txt'
    unrelated.write_text('unrelated')
    monkeypatch.setattr(device_events.sys, 'platform', 'win32')
    monkeypatch.setenv('SIDEPULSE_MOUNT_ROOTS', str(root))
    monkeypatch.setattr(device_events, 'default_mount_roots', lambda: (root,))
    monkeypatch.setattr(device_events, 'native_events', lambda changed: Mock())
    monkeypatch.setattr(device_events, 'Observer', lambda: Mock(ident=None))
    events = device_events.DeviceEvents()
    events.update()
    renamed = dot.rename(root / 'PulseDot')
    events.on_any_event(DirMovedEvent(str(dot), str(renamed)))
    events.changed.clear()
    unrelated.unlink()
    events.on_any_event(FileDeletedEvent(str(unrelated)))
    assert not events.changed.is_set()
    renamed.rmdir()
    events.on_any_event(FileDeletedEvent(str(renamed)))
    assert events.changed.is_set()


def test_watcher_start_failure_can_be_cleaned_up(watcher, monkeypatch):
    events, _ = watcher
    events.close()
    monkeypatch.setattr(device_events, 'native_events', lambda changed: Mock(
        start=Mock(side_effect=OSError('unavailable'))))
    failed = device_events.DeviceEvents()
    with pytest.raises(OSError):
        try:
            failed.start()
        finally:
            failed.close()
    assert not failed.observer.is_alive()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows drive semantics')
def test_windows_drive_is_itself_a_volume(tmp_path, monkeypatch):
    root = Path(tmp_path.anchor)
    assert tuple(device_writer.iter_mounts(root)) == (root,)
    assert root in device_writer.default_mount_roots()


def test_windows_roots_use_drive_letters(monkeypatch):
    monkeypatch.delenv('SIDEPULSE_MOUNT_ROOTS', raising=False)
    roots = (Path('C:/'), Path('E:/'))
    monkeypatch.setattr(device_writer, 'windows_drive_roots', lambda: roots)
    assert device_writer.default_mount_roots(platform='win32') == roots


def test_windows_missing_root_retains_armed_ancestor_watch(tmp_path, monkeypatch):
    from watchdog.events import DirCreatedEvent
    root = tmp_path / 'mounts'
    root.mkdir()
    missing = root / 'new-user' / 'volumes'
    monkeypatch.setattr(device_events.sys, 'platform', 'win32')
    monkeypatch.setenv('SIDEPULSE_MOUNT_ROOTS', str(missing))
    monkeypatch.setattr(device_events, 'default_mount_roots', lambda: (missing,))
    monkeypatch.setattr(device_events, 'native_events', lambda changed: Mock())
    observer = Mock(ident=None)
    monkeypatch.setattr(device_events, 'Observer', lambda: observer)
    events = device_events.DeviceEvents()
    events.update()
    assert set(events.watches) == {root}
    observer.schedule.assert_called_once_with(events, str(root), recursive=True)
    missing.mkdir(parents=True)
    events.update()
    assert set(events.watches) == {root}
    observer.unschedule.assert_not_called()
    events.changed.clear()
    dot = missing / 'SidePulseDot'
    dot.mkdir()
    events.on_any_event(DirCreatedEvent(str(dot)))
    assert events.changed.is_set()


def test_windows_root_recreation_keeps_the_parent_watch_armed(tmp_path, monkeypatch):
    from watchdog.events import FileDeletedEvent, DirCreatedEvent
    root = tmp_path / 'mounts'
    root.mkdir()
    monkeypatch.setattr(device_events.sys, 'platform', 'win32')
    monkeypatch.setenv('SIDEPULSE_MOUNT_ROOTS', str(root))
    monkeypatch.setattr(device_events, 'default_mount_roots', lambda: (root,))
    monkeypatch.setattr(device_events, 'native_events', lambda changed: Mock())
    observer = Mock(ident=None)
    monkeypatch.setattr(device_events, 'Observer', lambda: observer)
    events = device_events.DeviceEvents()
    events.update()
    assert set(events.watches) == {tmp_path}
    root.rmdir()
    events.on_any_event(FileDeletedEvent(str(root)))
    assert not events.reset_watches.is_set()
    events.update()
    events.changed.clear()
    root.mkdir()
    events.on_any_event(DirCreatedEvent(str(root)))
    assert events.changed.is_set()
    events.update()
    observer.schedule.assert_called_once_with(events, str(tmp_path), recursive=True)
    observer.unschedule.assert_not_called()
    observer.unschedule_all.assert_not_called()
