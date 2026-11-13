"""Relay a Windows ConPTY directly; OSC status uses the common parser."""
from __future__ import annotations

import os
import codecs
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid


def run_wrapped(command):
    import ctypes as c
    from ctypes import wintypes as w
    import msvcrt
    from winpty import PTY, Backend
    from .terminal_wrap import OscCapture, StatusForwarder, _write_all

    executable = shutil.which(command[0])
    if executable is None:
        raise FileNotFoundError(command[0])
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [w.DWORD]
    kernel.GetStdHandle.restype = w.HANDLE
    kernel.GetConsoleMode.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
    kernel.SetConsoleMode.argtypes = [w.HANDLE, w.DWORD]
    kernel.GetConsoleOutputCP.restype = w.UINT
    kernel.SetConsoleOutputCP.argtypes = [w.UINT]
    kernel.GetConsoleCP.restype = w.UINT
    kernel.SetConsoleCP.argtypes = [w.UINT]
    kernel.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, c.POINTER(w.DWORD), w.LPVOID]
    kernel.ReadFile.restype = w.BOOL
    kernel.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenThread.restype = w.HANDLE
    kernel.CancelSynchronousIo.argtypes = [w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    saved = []
    for descriptor, change in ((-10, lambda mode: (mode | 0x200) & ~7),
                               (-11, lambda mode: mode | 4)):
        handle = kernel.GetStdHandle(descriptor & 0xffffffff)
        mode = w.DWORD()
        if kernel.GetConsoleMode(handle, c.byref(mode)):
            if not kernel.SetConsoleMode(handle, change(mode.value)):
                raise c.WinError(c.get_last_error())
            saved.append((handle, mode.value))
    size = shutil.get_terminal_size((100, 30))
    old_output_mode = msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    old_output_cp = kernel.GetConsoleOutputCP()
    old_input_cp = kernel.GetConsoleCP()
    if old_output_cp:
        kernel.SetConsoleOutputCP(65001)
    if old_input_cp:
        kernel.SetConsoleCP(65001)
    child = None
    forwarder = StatusForwarder('wrap-' + uuid.uuid4().hex, command=command)
    outputs = queue.Queue(maxsize=64)
    closed = threading.Event()
    inputs = queue.Queue(maxsize=64)
    input_reader = None
    input_thread_handle = None
    write_lock = threading.Lock()

    def send(text):
        with write_lock:
            return child.write(text)

    def read():
        try:
            while not closed.is_set():
                text = child.read(blocking=True)
                if text:
                    outputs.put(text.encode('utf-8'))
                if child.iseof():
                    break
        except Exception as exc:
            if child.isalive():
                outputs.put(exc)
        finally:
            outputs.put(None)

    def read_input():
        # Read the VT byte stream, not CRT key events. getwch() loses parts of
        # cursor replies and Win32-input-mode sequences in nested ConPTYs.
        handle = kernel.GetStdHandle((-10) & 0xffffffff)
        buffer = c.create_string_buffer(4096)
        count = w.DWORD()
        while not closed.is_set():
            if not kernel.ReadFile(handle, buffer, len(buffer), c.byref(count), None):
                error = c.get_last_error()
                if closed.is_set() or error == 995:  # ERROR_OPERATION_ABORTED
                    return
                value = c.WinError(error)
            elif not count.value:
                return
            else:
                value = buffer.raw[:count.value]
            while not closed.is_set():
                try:
                    inputs.put(value, timeout=.1)
                    break
                except queue.Full:
                    pass
            if isinstance(value, Exception):
                return

    try:
        # Use PTY directly: PtyProcess internally bridges terminal output over
        # an unauthenticated loopback socket, which this relay does not need.
        child = PTY(size.columns, size.lines, backend=Backend.ConPTY)
        child.spawn(executable, cwd=os.getcwd(), env='\0'.join(f'{k}={v}' for k, v in os.environ.items()) + '\0',
                    cmdline=' ' + subprocess.list2cmdline(command[1:]) if len(command) > 1 else None)
        capture = OscCapture(forwarder.publish, lambda data: send(data.decode('ascii')))
        forwarder.start()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        if os.isatty(0):
            input_reader = threading.Thread(target=read_input, daemon=True)
            input_reader.start()
            input_thread_handle = kernel.OpenThread(1, False, input_reader.native_id)
            if not input_thread_handle:
                raise c.WinError(c.get_last_error())
        decode_input = codecs.getincrementaldecoder('utf-8')()
        while True:
            try:
                data = inputs.get_nowait()
            except queue.Empty:
                pass
            else:
                if isinstance(data, Exception):
                    raise data
                text = decode_input.decode(data)
                if text:
                    send(text)
            current = shutil.get_terminal_size((100, 30))
            if current != size:
                child.set_size(current.columns, current.lines)
                size = current
            try:
                data = outputs.get(timeout=.01)
            except queue.Empty:
                continue
            if data is None:
                break
            if isinstance(data, Exception):
                raise data
            _write_all(sys.stdout.fileno(), capture.feed(data))
        _write_all(sys.stdout.fileno(), capture.finish())
        while child.isalive():
            time.sleep(.01)
        return child.get_exitstatus() or 0
    finally:
        closed.set()
        if input_thread_handle:
            kernel.CancelSynchronousIo(input_thread_handle)
            input_reader.join(2)
            kernel.CloseHandle(input_thread_handle)
        if child is not None and child.isalive():
            # Terminate only the child owned by this invocation.
            child.write('\x03')
            deadline = time.monotonic() + 2
            while child.isalive() and time.monotonic() < deadline:
                time.sleep(.05)
            if child.isalive():
                os.kill(child.pid, 15)
        if forwarder.thread.ident is not None:
            forwarder.close()
        for handle, mode in saved:
            kernel.SetConsoleMode(handle, mode)
        msvcrt.setmode(sys.stdout.fileno(), old_output_mode)
        if old_output_cp:
            kernel.SetConsoleOutputCP(old_output_cp)
        if old_input_cp:
            kernel.SetConsoleCP(old_input_cp)
