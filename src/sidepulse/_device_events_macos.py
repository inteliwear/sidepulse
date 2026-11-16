"""macOS mount and USB notifications, sharing one blocking native run loop."""
import ctypes as C
import threading


class MacDeviceEvents:
    def __init__(self, changed):
        self.changed = changed
        self.ready = threading.Event()
        self.run_loop = None
        self.error = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name='sidepulse-devices', daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise OSError('Device notifications did not start')
        if self.error:
            raise OSError(str(self.error))

    def close(self):
        if self.run_loop:
            self.cf.CFRunLoopStop(self.run_loop)
            self.thread.join(2)

    def _run(self):
        port = session = source = None
        iterators = []
        try:
            self.cf = cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
            io = C.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
            da = C.CDLL('/System/Library/Frameworks/DiskArbitration.framework/DiskArbitration')
            def bind(lib, name, result, *args):
                fn = getattr(lib, name); fn.restype = result; fn.argtypes = list(args)
                return fn
            ptr, uint = C.c_void_p, C.c_uint
            bind(cf, 'CFRunLoopGetCurrent', ptr)
            bind(cf, 'CFRunLoopRun', None)
            bind(cf, 'CFRunLoopStop', None, ptr)
            bind(cf, 'CFRunLoopAddSource', None, ptr, ptr, ptr)
            bind(cf, 'CFRunLoopRemoveSource', None, ptr, ptr, ptr)
            bind(cf, 'CFRelease', None, ptr)
            mode = ptr.in_dll(cf, 'kCFRunLoopDefaultMode')
            self.run_loop = cf.CFRunLoopGetCurrent()
            bind(io, 'IONotificationPortCreate', ptr, uint)
            bind(io, 'IONotificationPortDestroy', None, ptr)
            bind(io, 'IONotificationPortGetRunLoopSource', ptr, ptr)
            bind(io, 'IOServiceMatching', ptr, C.c_char_p)
            bind(io, 'IOIteratorNext', uint, uint)
            bind(io, 'IOObjectRelease', C.c_int, uint)
            usb_callback = C.CFUNCTYPE(None, ptr, uint)
            bind(io, 'IOServiceAddMatchingNotification', C.c_int, ptr, C.c_char_p,
                 ptr, usb_callback, ptr, C.POINTER(uint))
            def usb_changed(_, iterator):
                while True:
                    device = io.IOIteratorNext(iterator)
                    if not device:
                        break
                    io.IOObjectRelease(device)
                    self.changed.set()
            self.usb_callback = usb_callback(usb_changed)
            port = io.IONotificationPortCreate(0)
            if not port:
                raise OSError('Cannot register USB notifications')
            source = io.IONotificationPortGetRunLoopSource(port)
            cf.CFRunLoopAddSource(self.run_loop, source, mode)
            for event in (b'IOServiceFirstMatch', b'IOServiceTerminate'):
                iterator = uint()
                result = io.IOServiceAddMatchingNotification(port, event,
                    io.IOServiceMatching(b'IOUSBHostDevice'), self.usb_callback, None, C.byref(iterator))
                if result:
                    raise OSError(f'Cannot register USB event: {result}')
                iterators.append(iterator.value)
                usb_changed(None, iterator.value)  # Drain to arm the notification.
            disk_callback = C.CFUNCTYPE(None, ptr, ptr)
            description_callback = C.CFUNCTYPE(None, ptr, ptr, ptr)
            self.disk_callback = disk_callback(lambda *_: self.changed.set())
            self.description_callback = description_callback(lambda *_: self.changed.set())
            bind(da, 'DASessionCreate', ptr, ptr)
            bind(da, 'DARegisterDiskAppearedCallback', None, ptr, ptr, disk_callback, ptr)
            bind(da, 'DARegisterDiskDisappearedCallback', None, ptr, ptr, disk_callback, ptr)
            bind(da, 'DARegisterDiskDescriptionChangedCallback', None, ptr, ptr, ptr, description_callback, ptr)
            bind(da, 'DASessionScheduleWithRunLoop', None, ptr, ptr, ptr)
            bind(da, 'DASessionUnscheduleFromRunLoop', None, ptr, ptr, ptr)
            session = da.DASessionCreate(None)
            if not session:
                raise OSError('Cannot register mount notifications')
            da.DARegisterDiskAppearedCallback(session, None, self.disk_callback, None)
            da.DARegisterDiskDisappearedCallback(session, None, self.disk_callback, None)
            da.DARegisterDiskDescriptionChangedCallback(session, None, None, self.description_callback, None)
            da.DASessionScheduleWithRunLoop(session, self.run_loop, mode)
            self.ready.set()
            cf.CFRunLoopRun()  # Sleeps until macOS delivers an event; no polling timer.
        except Exception as exc:
            self.error = exc
            self.ready.set()
        finally:
            if session:
                da.DASessionUnscheduleFromRunLoop(session, self.run_loop, mode)
                cf.CFRelease(session)
            if source:
                cf.CFRunLoopRemoveSource(self.run_loop, source, mode)
            for iterator in iterators:
                io.IOObjectRelease(iterator)
            if port:
                io.IONotificationPortDestroy(port)
            self.run_loop = None
