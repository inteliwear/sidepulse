"""Windows user identity, private files, and process-owned file locks."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import errno
import os
from pathlib import Path
import time


@lru_cache(maxsize=1)
def user_sid() -> str:
    import ctypes as c
    from ctypes import wintypes as w
    advapi, kernel = c.WinDLL('advapi32', use_last_error=True), c.WinDLL('kernel32', use_last_error=True)
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [c.c_void_p]
    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, c.byref(token)):
        raise c.WinError(c.get_last_error())
    try:
        size = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, c.byref(size))
        buffer = c.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, c.byref(size)):
            raise c.WinError(c.get_last_error())
        sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]
        text = w.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, c.byref(text)):
            raise c.WinError(c.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def private_directory(path: Path) -> None:
    """Protect credentials with a non-inheriting current-user/SYSTEM DACL."""
    import ctypes as c
    from ctypes import wintypes as w
    path.mkdir(parents=True, exist_ok=True)
    if path.lstat().st_file_attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        raise OSError('Private SidePulse directory cannot be a reparse point')
    api, kernel = c.WinDLL('advapi32', use_last_error=True), c.WinDLL('kernel32', use_last_error=True)
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p]
    api.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, c.c_void_p]
    kernel.LocalFree.argtypes = [c.c_void_p]
    descriptor = c.c_void_p()
    sddl = f'D:P(A;OICI;FA;;;{user_sid()})(A;OICI;FA;;;SY)'
    if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None):
        raise c.WinError(c.get_last_error())
    try:
        if not api.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise c.WinError(c.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


@contextmanager
def file_lock(path: Path, *, blocking: bool = True):
    """Byte zero is fixed, even after another process appends to the file."""
    import msvcrt
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if not path.stat().st_size:
            stream.write(b'\0')
            stream.flush()
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if not blocking or exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                time.sleep(.025)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def private_text(target: Path, text: str) -> None:
    """Atomically replace a credential file, private from its first byte."""
    import tempfile
    private_directory(target.parent)
    fd, name = tempfile.mkstemp(prefix='.' + target.name + '-', dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
