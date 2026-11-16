"""Device inventory invalidation, using native notifications rather than polling.

Directory watches cover mount folders and custom SIDEPULSE_MOUNT_ROOTS. OS
mount/USB events also cover mounts over existing folders and USB-only Dots.
Neither watcher reads device files or reacts to our LED writes.
"""
import os
from pathlib import Path
import sys
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .device_writer import default_mount_roots


def native_events(changed):
    if sys.platform == 'darwin':
        from ._device_events_macos import MacDeviceEvents
        return MacDeviceEvents(changed)
    if sys.platform.startswith('linux'):
        from ._device_events_linux import LinuxDeviceEvents
        return LinuxDeviceEvents(changed)
    if sys.platform == 'win32':
        from ._device_events_windows import WindowsDeviceEvents
        return WindowsDeviceEvents(changed)
    raise OSError(f'Device notifications unavailable on {sys.platform}')


class DeviceEvents(FileSystemEventHandler):
    def __init__(self):
        self.changed = threading.Event()
        self.changed.set()
        self.observer = Observer()
        self.native = native_events(self.changed)
        self.roots = ()
        self.watches = {}
        self.reset_watches = threading.Event()
        self.known_directories = set()

    def start(self):
        self.update()
        self.native.start()

    def update(self):
        # Re-evaluate after a connection event: Windows drive letters and Linux
        # per-user mount folders can appear/disappear while the daemon runs.
        # Mac/Windows already report volumes natively. Avoid recursively
        # watching Mac drives or holding directory handles on Windows drives.
        roots = default_mount_roots()
        if sys.platform in {'darwin', 'win32'} and 'SIDEPULSE_MOUNT_ROOTS' not in os.environ:
            roots = ()
        self.roots = tuple(Path(os.path.abspath(root)) for root in roots)
        if sys.platform == 'win32':
            # ReadDirectoryChangesW cannot classify a removed child after it
            # is gone. Remember directories when inventory is refreshed.
            self.known_directories = set(self.roots)
            for root in self.roots:
                try:
                    self.known_directories.update(path for path in root.iterdir() if path.is_dir())
                except OSError:
                    pass
        if self.reset_watches.is_set():
            self.reset_watches.clear()
            self.observer.unschedule_all()
            self.watches.clear()
        directories = set()
        for root in self.roots:
            # A Windows watch on the root itself dies when the root is
            # removed. Keep its existing parent armed through recreation.
            parent = root.parent if sys.platform == 'win32' else root
            while not parent.is_dir() and parent != parent.parent:
                parent = parent.parent
            if sys.platform == 'win32':
                # Keep an already armed ancestor watch when a configured
                # folder appears. Switching handles can lose arrivals between
                # registration and the first ReadDirectoryChangesW call.
                parent = next((watched for watched in self.watches
                    if watched.is_dir() and (watched == parent or watched in parent.parents)), parent)
            directories.add(parent)
        for directory in self.watches.keys() - directories:
            self.observer.unschedule(self.watches.pop(directory))
        for directory in directories - self.watches.keys():
            try:
                # FSEvents is intrinsically recursive; watchdog 6's shallow
                # filter drops direct child-directory events too. Filter paths
                # ourselves on macOS and Windows, keeping shallow watches on Linux.
                self.watches[directory] = self.observer.schedule(
                    self, str(directory), recursive=sys.platform in {'darwin', 'win32'})
            except OSError:
                # A mount can disappear between notification and registration.
                # Native removal/arrival events still invalidate discovery.
                pass
        if self.watches and self.observer.ident is None:
            self.observer.start()

    def on_any_event(self, event):
        if event.event_type not in {'created', 'deleted', 'moved'}:
            return
        source = Path(os.path.abspath(event.src_path))
        directory = event.is_directory or (sys.platform == 'win32' and
            event.event_type == 'deleted' and source in self.known_directories)
        if not directory:
            return
        if sys.platform == 'win32':
            self.known_directories.discard(source)
            if event.event_type == 'created':
                self.known_directories.add(source)
            elif event.event_type == 'moved' and event.dest_path:
                self.known_directories.add(Path(os.path.abspath(event.dest_path)))
        for value in (event.src_path, getattr(event, 'dest_path', '')):
            if not value:
                continue
            path = Path(os.path.abspath(value))
            # FSEvents may include the watched folder's creation history. It
            # already existed when we registered, so inventory covers it.
            if event.event_type == 'created' and path in self.watches:
                continue
            if event.event_type in {'deleted', 'moved'} and path in self.watches:
                self.reset_watches.set()
            if any(path == root or path.parent == root or
                   (path in root.parents and (path not in self.watches or
                    event.event_type in {'deleted', 'moved'})) for root in self.roots):
                self.changed.set()
                return

    def close(self):
        self.native.close()
        self.observer.stop()
        if self.observer.ident is not None:
            self.observer.join(2)
