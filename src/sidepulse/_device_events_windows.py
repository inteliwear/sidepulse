"""Windows device broadcasts on a hidden top-level window (no polling)."""
import ctypes as C
from ctypes import wintypes as W
import threading


class WindowsDeviceEvents:
    def __init__(self, changed):
        self.changed = changed
        self.ready = threading.Event()
        self.window = None
        self.error = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name='sidepulse-devices', daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise OSError('Device notifications did not start')
        if self.error:
            raise OSError(str(self.error))

    def close(self):
        if self.window:
            self.user.PostMessageW(self.window, 0x0010, 0, 0)  # WM_CLOSE
        if self.thread:
            self.thread.join(2)

    def _run(self):
        try:
            self._listen()
        except Exception as exc:
            self.error = exc
            self.ready.set()

    def _listen(self):
        user = self.user = C.WinDLL('user32', use_last_error=True)
        kernel = C.WinDLL('kernel32', use_last_error=True)
        callback = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

        class WindowClass(C.Structure):
            _fields_ = [('style', W.UINT), ('procedure', callback), ('class_extra', C.c_int),
                        ('window_extra', C.c_int), ('instance', W.HINSTANCE), ('icon', W.HICON),
                        ('cursor', W.HANDLE), ('background', W.HBRUSH),
                        ('menu_name', W.LPCWSTR), ('class_name', W.LPCWSTR)]

        def bind(library, name, result, *args):
            fn = getattr(library, name)
            fn.restype, fn.argtypes = result, list(args)
            return fn

        bind(kernel, 'GetModuleHandleW', W.HMODULE, W.LPCWSTR)
        bind(user, 'RegisterClassW', W.ATOM, C.POINTER(WindowClass))
        bind(user, 'UnregisterClassW', W.BOOL, W.LPCWSTR, W.HINSTANCE)
        bind(user, 'CreateWindowExW', W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
             C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p)
        bind(user, 'DefWindowProcW', C.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        bind(user, 'PostMessageW', W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        bind(user, 'DestroyWindow', W.BOOL, W.HWND)
        bind(user, 'GetMessageW', W.BOOL, C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
        bind(user, 'TranslateMessage', W.BOOL, C.POINTER(W.MSG))
        bind(user, 'DispatchMessageW', C.c_ssize_t, C.POINTER(W.MSG))
        bind(user, 'PostQuitMessage', None, C.c_int)

        @callback
        def procedure(window, message, wparam, lparam):
            if message == 0x0219:  # WM_DEVICECHANGE: USB and volume broadcasts.
                if wparam in (0x0007, 0x8000, 0x8004):  # nodes changed, arrival, removal
                    self.changed.set()
                return 1
            if message == 0x0010:  # WM_CLOSE
                user.DestroyWindow(window)
                return 0
            if message == 0x0002:  # WM_DESTROY
                self.window = None
                user.PostQuitMessage(0)
                return 0
            return user.DefWindowProcW(window, message, wparam, lparam)

        instance = kernel.GetModuleHandleW(None)
        name = f'SidePulseDevices-{id(self)}'
        registered = False
        try:
            wc = WindowClass(procedure=procedure, instance=instance, class_name=name)
            if not user.RegisterClassW(C.byref(wc)):
                raise C.WinError(C.get_last_error())
            registered = True
            # A message-only window does not receive device broadcasts. This
            # ordinary top-level window remains hidden (no WS_VISIBLE style).
            self.window = user.CreateWindowExW(0, name, name, 0, 0, 0, 0, 0,
                                              None, None, instance, None)
            if not self.window:
                raise C.WinError(C.get_last_error())
            self.ready.set()
            msg = W.MSG()
            while (result := user.GetMessageW(C.byref(msg), None, 0, 0)) > 0:
                user.TranslateMessage(C.byref(msg))
                user.DispatchMessageW(C.byref(msg))
            if result == -1:
                raise C.WinError(C.get_last_error())
        finally:
            if self.window:
                user.DestroyWindow(self.window)
                self.window = None
            if registered:
                user.UnregisterClassW(name, instance)
