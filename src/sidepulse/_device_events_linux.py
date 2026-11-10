"""Blocking Linux mount-table and USB uevent notifications."""
import os
import select
import socket
import threading


class LinuxDeviceEvents:
    def __init__(self, changed):
        self.changed = changed
        self.thread = None
        self.mounts = self.usb = None
        self.stop_read = self.stop_write = None

    def start(self):
        self.mounts = open('/proc/self/mounts', 'rb', buffering=0)
        self.mounts.read()  # Establish the initial mount-table generation.
        self.usb = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 15)  # NETLINK_KOBJECT_UEVENT
        self.usb.bind((0, 1))
        self.stop_read, self.stop_write = os.pipe()
        self.thread = threading.Thread(target=self._run, name='sidepulse-devices', daemon=True)
        self.thread.start()

    def _run(self):
        poll = select.poll()
        poll.register(self.mounts, select.POLLPRI)
        poll.register(self.usb, select.POLLIN)
        poll.register(self.stop_read, select.POLLIN)
        while True:
            for fd, _ in poll.poll():  # No timeout: the kernel wakes us.
                if fd == self.stop_read:
                    return
                if fd == self.mounts.fileno():
                    self.mounts.seek(0)
                    self.mounts.read()
                    self.changed.set()
                elif fd == self.usb.fileno():
                    message = self.usb.recv(65536).split(b'\0')
                    if (b'SUBSYSTEM=usb' in message and b'DEVTYPE=usb_device' in message
                            and any(action in message for action in (b'ACTION=add', b'ACTION=remove'))):
                        self.changed.set()

    def close(self):
        if self.thread:
            os.write(self.stop_write, b'!')
            self.thread.join(2)
            self.thread = None
        for stream in (self.mounts, self.usb):
            if stream is not None:
                stream.close()
        for fd in (self.stop_read, self.stop_write):
            if fd is not None:
                os.close(fd)
        self.mounts = self.usb = self.stop_read = self.stop_write = None
