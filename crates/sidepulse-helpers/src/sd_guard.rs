//! DiskArbitration stays on one CoreFoundation run loop. No background thread
//! owns a disk reference or the callback context.
use core_foundation::{base::TCFType, string::CFString};
use core_foundation_sys::{
    base::{CFAllocatorRef, CFRelease, CFRetain, CFTypeRef},
    dictionary::{CFDictionaryGetValue, CFDictionaryRef},
    runloop::{CFRunLoopGetCurrent, CFRunLoopRef, CFRunLoopRunInMode, kCFRunLoopDefaultMode},
    string::CFStringRef,
};
use std::{
    collections::BTreeMap,
    ffi::{CStr, c_char, c_void},
    io::{self, Write},
    ptr,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

type Disk = *const c_void;
type Session = *const c_void;
type Dissenter = *const c_void;
#[link(name = "DiskArbitration", kind = "framework")]
unsafe extern "C" {
    fn DASessionCreate(allocator: CFAllocatorRef) -> Session;
    fn DASessionScheduleWithRunLoop(session: Session, run_loop: CFRunLoopRef, mode: CFStringRef);
    fn DASessionUnscheduleFromRunLoop(session: Session, run_loop: CFRunLoopRef, mode: CFStringRef);
    fn DARegisterDiskEjectApprovalCallback(
        session: Session,
        matched: CFDictionaryRef,
        callback: unsafe extern "C" fn(Disk, *mut c_void) -> Dissenter,
        context: *mut c_void,
    );
    fn DARegisterDiskDisappearedCallback(
        session: Session,
        matched: CFDictionaryRef,
        callback: unsafe extern "C" fn(Disk, *mut c_void),
        context: *mut c_void,
    );
    fn DAUnregisterCallback(session: Session, callback: *const c_void, context: *mut c_void);
    fn DADiskCopyDescription(disk: Disk) -> CFDictionaryRef;
    fn DADiskGetBSDName(disk: Disk) -> *const c_char;
    fn DADiskMount(
        disk: Disk,
        path: *const c_void,
        options: u32,
        callback: Option<unsafe extern "C" fn(Disk, Dissenter, *mut c_void)>,
        context: *mut c_void,
    );
    fn DADissenterCreate(allocator: CFAllocatorRef, status: i32, message: CFStringRef)
    -> Dissenter;
    static kDADiskDescriptionDeviceProtocolKey: CFStringRef;
    static kDADiskDescriptionDeviceModelKey: CFStringRef;
    static kDADiskDescriptionVolumePathKey: CFStringRef;
    static kDADiskDescriptionVolumeNameKey: CFStringRef;
}
struct OwnedCf(CFTypeRef);
impl Drop for OwnedCf {
    fn drop(&mut self) {
        unsafe {
            CFRelease(self.0);
        }
    }
}
fn session() -> io::Result<OwnedCf> {
    let session = unsafe { DASessionCreate(ptr::null()) };
    if session.is_null() {
        Err(io::Error::other("DASessionCreate failed"))
    } else {
        Ok(OwnedCf(session))
    }
}
pub fn check() -> io::Result<()> {
    let _session = session()?;
    Ok(())
}
fn description(disk: Disk) -> Option<OwnedCf> {
    let value = unsafe { DADiskCopyDescription(disk) };
    if value.is_null() {
        None
    } else {
        Some(OwnedCf(value.cast()))
    }
}
fn value(description: &OwnedCf, key: CFStringRef) -> CFTypeRef {
    unsafe { CFDictionaryGetValue(description.0.cast(), key.cast()) }
}
fn string(description: &OwnedCf, key: CFStringRef) -> Option<String> {
    let value = value(description, key);
    if value.is_null() {
        None
    } else {
        Some(unsafe { CFString::wrap_under_get_rule(value.cast()) }.to_string())
    }
}
fn name(disk: Disk) -> String {
    let name = unsafe { DADiskGetBSDName(disk) };
    if name.is_null() {
        "?".into()
    } else {
        unsafe { CStr::from_ptr(name) }
            .to_string_lossy()
            .into_owned()
    }
}
struct Retry {
    disk: OwnedCf,
    last_attempt: Instant,
}
struct Context {
    no_mount: bool,
    retries: BTreeMap<String, Retry>,
}
unsafe extern "C" fn eject(disk: Disk, context: *mut c_void) -> Dissenter {
    let Some(description) = description(disk) else {
        return ptr::null();
    };
    let protocol = string(&description, unsafe { kDADiskDescriptionDeviceProtocolKey });
    let model = string(&description, unsafe { kDADiskDescriptionDeviceModelKey });
    if !super::is_builtin_sd(protocol.as_deref(), model.as_deref()) {
        return ptr::null();
    }
    let context = unsafe { &mut *context.cast::<Context>() };
    let name = name(disk);
    let volume = string(&description, unsafe { kDADiskDescriptionVolumeNameKey })
        .unwrap_or_else(|| "?".into());
    truncate_log();
    let mut stdout = io::stdout().lock();
    let _ = writeln!(stdout, "prevented eject of {name} (volume: {volume})");
    let _ = stdout.flush();
    drop(stdout);
    if !context.no_mount && !context.retries.contains_key(&name) && context.retries.len() < 128 {
        let retained = unsafe { CFRetain(disk) };
        context.retries.insert(
            name,
            Retry {
                disk: OwnedCf(retained),
                last_attempt: Instant::now(),
            },
        );
    }
    let reason = CFString::new("SidePulse Pro Eject Prevention: keeping SD card attached");
    // kDAReturnNotPermitted from the installed DiskArbitration SDK.
    unsafe {
        DADissenterCreate(
            ptr::null(),
            0xF8DA0008_u32 as i32,
            reason.as_concrete_TypeRef(),
        )
    }
}
unsafe extern "C" fn disappeared(disk: Disk, context: *mut c_void) {
    unsafe { &mut *context.cast::<Context>() }
        .retries
        .remove(&name(disk));
}
unsafe extern "C" fn mount_done(_disk: Disk, _dissenter: Dissenter, _context: *mut c_void) {}
fn truncate_log() {
    let _ = io::stdout().flush();
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    // Only truncate a regular redirected stdout log, never a terminal or pipe.
    unsafe {
        if libc::fstat(libc::STDOUT_FILENO, stat.as_mut_ptr()) == 0 {
            let stat = stat.assume_init();
            if stat.st_mode & libc::S_IFMT == libc::S_IFREG
                && stat.st_size > 10 * 1024 * 1024
                && libc::ftruncate(libc::STDOUT_FILENO, 0) == 0
            {
                libc::lseek(libc::STDOUT_FILENO, 0, libc::SEEK_SET);
            }
        }
    }
}
pub fn run(no_mount: bool) -> io::Result<()> {
    let session = session()?;
    let stop = Arc::new(AtomicBool::new(false));
    let signal = stop.clone();
    ctrlc::set_handler(move || signal.store(true, Ordering::Release)).map_err(io::Error::other)?;
    let mut context = Box::new(Context {
        no_mount,
        retries: BTreeMap::new(),
    });
    let opaque = (&mut *context as *mut Context).cast::<c_void>();
    let run_loop = unsafe { CFRunLoopGetCurrent() };
    unsafe {
        DARegisterDiskEjectApprovalCallback(session.0, ptr::null(), eject, opaque);
        DARegisterDiskDisappearedCallback(session.0, ptr::null(), disappeared, opaque);
        DASessionScheduleWithRunLoop(session.0, run_loop, kCFRunLoopDefaultMode);
    }
    while !stop.load(Ordering::Acquire) {
        unsafe {
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 1.0, 0);
        }
        context.retries.retain(|_, retry| {
            description(retry.disk.0).is_none_or(|description| {
                value(&description, unsafe { kDADiskDescriptionVolumePathKey }).is_null()
            })
        });
        let mut due = Vec::new();
        for retry in context.retries.values_mut() {
            if retry.last_attempt.elapsed() >= Duration::from_secs(5) {
                retry.last_attempt = Instant::now();
                due.push(OwnedCf(unsafe { CFRetain(retry.disk.0) }));
            }
        }
        // Release the mutable context borrow before invoking an API that can
        // cause callbacks. Each attempt retains its disk through that call.
        for disk in due {
            unsafe {
                DADiskMount(disk.0, ptr::null(), 0, Some(mount_done), ptr::null_mut());
            }
        }
    }
    unsafe {
        DASessionUnscheduleFromRunLoop(session.0, run_loop, kCFRunLoopDefaultMode);
        DAUnregisterCallback(session.0, eject as *const c_void, opaque);
        DAUnregisterCallback(session.0, disappeared as *const c_void, opaque);
    }
    Ok(())
}
