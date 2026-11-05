"""PTY relay with bounded OSC capture; the daemon remains the status owner."""
from __future__ import annotations

import errno
import os
import select
import signal
import sys
import threading
import time
import uuid

from .osc7501 import MAX_OSC_BYTES, parse_osc7501
from .program_transport import submit_status

PREFIX = b'\x1b]7501;'
CAPABILITY_REPLY = b'\x1b]7501;?:states=idle,working,done,blocked,error:kinds=permission,question,auth\x1b\\'


class OscCapture:
    """Inspect top-level OSCs, retaining at most one bounded candidate.

    Output is unchanged except that capability queries are consumed and answered
    locally. Other control strings are opaque, including embedded OSC-looking
    bytes in DCS/tmux passthrough. Incomplete reports never become status.
    """
    def __init__(self, on_status, on_query):
        self.on_status, self.on_query = on_status, on_query
        self.mode = 'text'
        self.buffer = bytearray()
        self.osc_string = False
        self.string_escape = False

    def feed(self, data: bytes) -> bytes:
        output = bytearray()
        for byte in data:
            if self.mode == 'string':
                output.append(byte)
                if byte in (0x18, 0x1a) or (self.osc_string and byte == 7) or (self.string_escape and byte == 92):
                    self.mode = 'text'
                self.string_escape = byte == 27
            elif self.mode == 'text':
                if byte == 27:
                    self.buffer.append(byte)
                    self.mode = 'escape'
                else:
                    output.append(byte)
            elif self.mode == 'escape':
                if byte == 93:  # ESC ] introduces OSC.
                    self.buffer.append(byte)
                    self.mode = 'prefix'
                else:
                    output.extend(self.buffer)
                    self.buffer.clear()
                    if byte == 27:
                        self.buffer.append(byte)
                    else:
                        output.append(byte)
                        self.mode = 'string' if byte in (80, 88, 94, 95) else 'text'
                        self.osc_string = False
                        self.string_escape = False
            elif self.mode == 'prefix':
                self.buffer.append(byte)
                if self.buffer == PREFIX:
                    self.mode = 'report'
                elif not PREFIX.startswith(self.buffer):
                    output.extend(self.buffer)
                    self.buffer.clear()
                    self.mode = 'text' if byte in (7, 0x18, 0x1a) else 'string'
                    self.osc_string = True
                    self.string_escape = byte == 27
            else:  # OSC 7501 candidate.
                previous_escape = self.buffer[-1] == 27
                self.buffer.append(byte)
                complete = byte == 7 or (previous_escape and byte == 92)
                if len(self.buffer) > MAX_OSC_BYTES or byte in (0x18, 0x1a):
                    output.extend(self.buffer)
                    self.buffer.clear()
                    self.mode = 'text' if complete or byte in (0x18, 0x1a) else 'string'
                    self.osc_string = True
                    self.string_escape = byte == 27
                elif complete:
                    raw = bytes(self.buffer)
                    self.buffer.clear()
                    self.mode = 'text'
                    body = raw[len(PREFIX):-1 if byte == 7 else -2]
                    if body.strip() == b'?':
                        self.on_query(CAPABILITY_REPLY)
                    else:
                        output.extend(raw)
                        try:
                            status = parse_osc7501(raw.decode('ascii'))
                        except (ValueError, UnicodeError):
                            continue
                        self.on_status(status)
        return bytes(output)

    def finish(self) -> bytes:
        pending = bytes(self.buffer)
        self.buffer.clear()
        self.mode = 'text'
        return pending


class StatusForwarder:
    """One pending value per invocation; daemon I/O never runs in the PTY loop."""
    def __init__(self, scope: str, *, submit=None):
        self.scope = scope
        self.submit = submit or submit_status
        self.condition = threading.Condition()
        self.pending = None
        self.last_state = None
        self.closed = False
        self.thread = threading.Thread(target=self._run, name='sidepulse-wrap-status', daemon=True)

    def start(self):
        self.thread.start()

    def publish(self, status: dict):
        with self.condition:
            self.last_state = status['state']
            self.pending = {'v': 1, 'type': 'program_status', 'scope': self.scope, 'status': status}
            self.condition.notify()

    def close(self):
        # Process exit ends live work, but explicit results remain available.
        with self.condition:
            if self.last_state in {'working', 'blocked', 'idle'}:
                self.publish({'state': 'clear'})
            self.closed = True
            self.condition.notify()
        self.thread.join(timeout=1)

    def _run(self):
        warned = False
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending is not None or self.closed)
                report, self.pending = self.pending, None
                if report is None:
                    return
            try:
                self.submit(report, timeout=0.2)
            except (OSError, ValueError):
                if not warned:
                    print('SidePulse status unavailable; the wrapped command will continue.', file=sys.stderr)
                    warned = True


def _write_all(fd: int, data: bytes):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if not written:
            raise OSError(errno.EIO, 'Terminal output closed')
        view = view[written:]


def run_wrapped(command: list[str]) -> int:
    if os.name != 'posix':
        raise ValueError('Terminal wrapping is supported on macOS and Linux')
    # Keep Unix-only dependencies out of imports on unsupported platforms.
    import fcntl
    import pty
    import termios
    import tty

    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()
    terminal = termios.tcgetattr(stdin) if os.isatty(stdin) else None
    size_fd = next((fd for fd in (stdin, stdout, sys.stderr.fileno()) if os.isatty(fd)), None)
    size = fcntl.ioctl(size_fd, termios.TIOCGWINSZ, b'\0' * 8) if size_fd is not None else None
    # Fork before starting our forwarding thread. No shell is involved.
    ready_read, ready_write = os.pipe()
    try:
        pid, master = pty.fork()
    except OSError:
        os.close(ready_read)
        os.close(ready_write)
        raise
    if pid == 0:
        try:
            os.close(ready_read)
            if terminal is not None:
                termios.tcsetattr(0, termios.TCSANOW, terminal)
            else:
                attributes = termios.tcgetattr(0)
                attributes[3] &= ~termios.ECHO
                termios.tcsetattr(0, termios.TCSANOW, attributes)
            if size is not None:
                fcntl.ioctl(0, termios.TIOCSWINSZ, size)
            os.write(ready_write, b'1')
            os.close(ready_write)
            for name in ('SIGPIPE', 'SIGXFZ', 'SIGXFSZ'):
                if hasattr(signal, name):
                    signal.signal(getattr(signal, name), signal.SIG_DFL)
            os.execvp(command[0], command)
        except BaseException as exc:
            # A failed exec must never return into the parent's Python caller.
            detail = exc.strerror if isinstance(exc, OSError) else str(exc)
            try:
                _write_all(2, f'sidepulse wrap: {command[0]}: {detail}\n'.encode(errors='replace'))
            finally:
                os._exit(127 if isinstance(exc, OSError) and exc.errno == errno.ENOENT else 126)

    os.close(ready_write)
    forwarder = StatusForwarder('wrap-' + uuid.uuid4().hex)
    outgoing = bytearray()

    def reply(data):
        if len(outgoing) + len(data) <= 65536:
            outgoing.extend(data)

    capture = OscCapture(forwarder.publish, reply)
    old_handlers = {}
    child_status = None
    exit_deadline = None
    input_open = True
    master_open = True
    suspend = False

    def forward_signal(signum, _frame=None):
        try:
            group = os.tcgetpgrp(master)
            os.killpg(group if group > 0 else pid, signum)
        except (OSError, ProcessLookupError):
            try:
                os.killpg(pid, signum)
            except ProcessLookupError:
                pass

    def resize(_signum=None, _frame=None):
        if size_fd is not None:
            try:
                current = fcntl.ioctl(size_fd, termios.TIOCGWINSZ, b'\0' * 8)
                fcntl.ioctl(master, termios.TIOCSWINSZ, current)
            except OSError:
                pass

    def stop(_signum, _frame):
        nonlocal suspend
        forward_signal(signal.SIGTSTP)
        suspend = True

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            old_handlers[signum] = signal.signal(signum, forward_signal)
        old_handlers[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, resize)
        old_handlers[signal.SIGTSTP] = signal.signal(signal.SIGTSTP, stop)
        # Do not send redirected input/EOF before the slave's echo is disabled.
        os.read(ready_read, 1)
        os.close(ready_read)
        ready_read = None
        os.set_blocking(master, False)
        if terminal is not None:
            tty.setraw(stdin, termios.TCSANOW)
        resize()
        forwarder.start()
        while True:
            if child_status is None:
                waited, status = os.waitpid(pid, os.WNOHANG | os.WUNTRACED)
                if waited:
                    if os.WIFSTOPPED(status):
                        suspend = True
                    else:
                        child_status = status
                        exit_deadline = time.monotonic() + 0.2
            if suspend and child_status is None:
                if terminal is not None:
                    termios.tcsetattr(stdin, termios.TCSANOW, terminal)
                os.kill(os.getpid(), signal.SIGSTOP)
                if terminal is not None:
                    tty.setraw(stdin, termios.TCSANOW)
                resize()
                forward_signal(signal.SIGCONT)
                suspend = False
            if child_status is not None and (not master_open or time.monotonic() >= exit_deadline):
                break
            readers = [master] if master_open else []
            # Bound queued keyboard input if the child stops reading.
            if master_open and input_open and child_status is None and len(outgoing) < 65536:
                readers.append(stdin)
            ready, writable, _ = select.select(readers, [master] if outgoing and master_open else [], [], 0.05)
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:  # Linux PTY EOF.
                        master_open = False
                    elif exc.errno != errno.EAGAIN:
                        raise
                else:
                    if not data:
                        master_open = False
                    else:
                        _write_all(stdout, capture.feed(data))
            if master_open and stdin in ready:
                data = os.read(stdin, 4096)
                if data:
                    outgoing.extend(data)
                else:
                    input_open = False
                    # A PTY has no write-half close. Two VEOFs flush a partial
                    # canonical line and then deliver EOF to its reader.
                    eof = termios.tcgetattr(master)[6][termios.VEOF]
                    outgoing.extend(eof * 2)
            if master in writable and outgoing:
                try:
                    written = os.write(master, outgoing)
                    del outgoing[:written]
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        master_open = False
                    elif exc.errno != errno.EAGAIN:
                        raise
        _write_all(stdout, capture.finish())
        code = os.waitstatus_to_exitcode(child_status)
        return code if code >= 0 else 128 - code
    finally:
        if ready_read is not None:
            os.close(ready_read)
        if terminal is not None:
            try:
                termios.tcsetattr(stdin, termios.TCSANOW, terminal)
            except OSError:
                pass  # A disconnected outer terminal must not skip cleanup.
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        if child_status is None:
            forward_signal(signal.SIGKILL)
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(master)
        if forwarder.thread.ident is not None:
            forwarder.close()
