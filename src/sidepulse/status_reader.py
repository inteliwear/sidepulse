"""Fresh, read-only device file reads, including cached macOS volumes."""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


class _MacFile:
    """Read-only, aligned positional I/O; also used for fresh ordinary file reads."""
    def __init__(self, path: Path, expected_size=None):
        if sys.platform != "darwin":
            raise OSError("READ2ME hardware transport currently supports macOS only")
        import fcntl
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.posix_memalign.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t, ctypes.c_size_t]
        self.libc.pread.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_longlong]
        self.libc.pread.restype = ctypes.c_ssize_t
        self.libc.free.argtypes = [ctypes.c_void_p]
        self.buffer = ctypes.c_void_p()
        self.fd = os.open(path, os.O_RDONLY)
        try:
            if expected_size is not None and os.fstat(self.fd).st_size != expected_size:
                raise ValueError(f"control file must be exactly {expected_size} bytes")
            fcntl.fcntl(self.fd, 48, 1)  # Darwin F_NOCACHE
            fcntl.fcntl(self.fd, 45, 0)  # Darwin F_RDAHEAD
            # F_NOCACHE alone is insufficient: an unaligned Python bytes buffer
            # makes macOS read surrounding sectors, corrupting address symbols.
            page_size = os.sysconf("SC_PAGESIZE")
            result = self.libc.posix_memalign(ctypes.byref(self.buffer), page_size, page_size)
            if result:
                raise OSError(result, os.strerror(result))
        except BaseException:
            os.close(self.fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.fd < 0:
            return
        self.libc.free(self.buffer)
        self.buffer = ctypes.c_void_p()
        os.close(self.fd)
        self.fd = -1

    def invalidate_cache(self):
        """Evict existing cached pages before reading a device file."""
        if self.fd < 0:
            raise ValueError("channel is closed")
        size = os.fstat(self.fd).st_size
        if size == 0:
            return
        self.libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_longlong]
        self.libc.mmap.restype = ctypes.c_void_p
        self.libc.msync.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        self.libc.msync.restype = ctypes.c_int
        self.libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self.libc.munmap.restype = ctypes.c_int
        # PROT_READ | MAP_SHARED; never access the mapping itself.
        address = self.libc.mmap(None, size, 1, 1, self.fd, 0)
        if address == ctypes.c_void_p(-1).value:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        try:
            if self.libc.msync(address, size, 2) != 0:  # MS_INVALIDATE
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        finally:
            if self.libc.munmap(address, size) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))

    def read_at(self, offset: int) -> bytes:
        if self.fd < 0:
            raise ValueError("channel is closed")
        count = self.libc.pread(self.fd, self.buffer, 512, offset)
        if count < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return ctypes.string_at(self.buffer, count)



def read_status_bytes(path: Path, *, max_bytes: int = 4096) -> bytes:
    """Read a device file with the read-only, uncached macOS transport."""
    if sys.platform == "darwin":
        with _MacFile(path) as stream:
            stream.invalidate_cache()
            data = b""
            for offset in range(0, max_bytes, 512):
                block = stream.read_at(offset)
                data += block[:max_bytes - offset]
                if len(block) < 512:
                    break
    else:
        with path.open("rb") as stream:
            data = stream.read(max_bytes)
    return data
