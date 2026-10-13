// SidePulse Pro Eject Prevention - veto software ejects of cards in the built-in SD reader.
//
// macOS loginwindow dissents the mount and ejects any disk that appears
// while the screen is locked (e.g. the SD slot re-enumerating after a
// hibernate wake). The eject goes through the same DiskArbitration approval
// mechanism, so a registered client can dissent it right back. This tool does
// that, then retries the mount every few seconds; the retries are dissented
// while locked and succeed after unlock.
// A Disk Arbitration daemon restart can leave this session registered only
// with the old daemon. Exit when its PID changes so launchd starts a fresh one.
//
// Build: clang -o "SidePulse Pro Eject Prevention" sd_eject_guard.c \
//          -framework DiskArbitration -framework CoreFoundation
// Run:   ./sd_eject_guard [-n]   (leave running; Ctrl-C to stop)
//        -n / --no-mount: only veto ejects; don't retry mounting.

#include <CoreFoundation/CoreFoundation.h>
#include <DiskArbitration/DiskArbitration.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define MOUNT_RETRY_SECONDS 5.0
#define DA_PID_CHECK_SECONDS 0.5
#define LOG_TRUNCATE_BYTES (10 * 1024 * 1024)
#ifndef SD_EJECT_GUARD_DA_PIDFILE
#define SD_EJECT_GUARD_DA_PIDFILE "/var/run/diskarbitrationd.pid"
#endif

static DASessionRef g_session;
static bool g_no_mount = false;
static long g_da_pid = 0;

static long disk_arbitration_pid(void) {
    FILE *file = fopen(SD_EJECT_GUARD_DA_PIDFILE, "r");
    if (!file) return 0;
    long pid = 0;
    if (fscanf(file, "%ld", &pid) != 1 || pid <= 0) pid = 0;
    fclose(file);
    return pid;
}

static void check_disk_arbitration(CFRunLoopTimerRef timer, void *ctx) {
    (void)timer;
    (void)ctx;
    long pid = disk_arbitration_pid();
    if (g_da_pid > 0 && pid > 0 && pid != g_da_pid) {
        fprintf(stderr, "diskarbitrationd changed from %ld to %ld; restarting guard\n",
                g_da_pid, pid);
        fflush(stderr);
        // launchd KeepAlive starts a new process with fresh DA callbacks.
        exit(0);
    }
    if (g_da_pid == 0) g_da_pid = pid;
}

static void truncate_stdout_if_large(void) {
    fflush(stdout);
    int fd = fileno(stdout);
    struct stat st;
    if (fd < 0 || fstat(fd, &st) != 0 || !S_ISREG(st.st_mode))
        return;
    if (st.st_size <= LOG_TRUNCATE_BYTES)
        return;
    if (ftruncate(fd, 0) == 0)
        lseek(fd, 0, SEEK_SET);
    clearerr(stdout);
}

static void log_msg(const char *fmt, CFStringRef s) {
    char buf[256] = "?";
    if (s) CFStringGetCString(s, buf, sizeof(buf), kCFStringEncodingUTF8);
    printf(fmt, buf);
    fflush(stdout);
}

static bool is_builtin_sd(DADiskRef disk) {
    CFDictionaryRef desc = DADiskCopyDescription(disk);
    if (!desc) return false;
    CFStringRef proto = CFDictionaryGetValue(desc, kDADiskDescriptionDeviceProtocolKey);
    CFStringRef model = CFDictionaryGetValue(desc, kDADiskDescriptionDeviceModelKey);
    bool match =
        (proto && CFStringFind(proto, CFSTR("Secure Digital"), 0).location != kCFNotFound) ||
        (model && CFStringFind(model, CFSTR("SDXC"), 0).location != kCFNotFound);
    CFRelease(desc);
    return match;
}

static bool is_mounted(DADiskRef disk) {
    CFDictionaryRef desc = DADiskCopyDescription(disk);
    if (!desc) return false;
    bool mounted = CFDictionaryGetValue(desc, kDADiskDescriptionVolumePathKey) != NULL;
    CFRelease(desc);
    return mounted;
}

static void mount_done(DADiskRef disk, DADissenterRef dissenter, void *ctx);

static void retry_mount(CFRunLoopTimerRef timer, void *info) {
    DADiskRef disk = (DADiskRef)info;
    if (is_mounted(disk)) {
        CFRunLoopTimerInvalidate(timer);
        CFRelease(disk);
        return;
    }
    DADiskMount(disk, NULL, kDADiskMountOptionDefault, mount_done, NULL);
}

static void mount_done(DADiskRef disk, DADissenterRef dissenter, void *ctx) {
    (void)disk;
    (void)dissenter;
    (void)ctx;
}

static void start_mount_retries(DADiskRef disk) {
    CFRetain(disk);
    CFRunLoopTimerContext tctx = { 0, (void *)disk, NULL, NULL, NULL };
    CFRunLoopTimerRef timer = CFRunLoopTimerCreate(
        kCFAllocatorDefault, CFAbsoluteTimeGetCurrent() + MOUNT_RETRY_SECONDS,
        MOUNT_RETRY_SECONDS, 0, 0, retry_mount, &tctx);
    CFRunLoopAddTimer(CFRunLoopGetCurrent(), timer, kCFRunLoopDefaultMode);
    CFRelease(timer);
}

static DADissenterRef eject_approval(DADiskRef disk, void *ctx) {
    (void)ctx;
    if (!is_builtin_sd(disk))
        return NULL;  // not ours, allow
    const char *disk_name = DADiskGetBSDName(disk);
    if (!disk_name) disk_name = "?";
    CFDictionaryRef desc = DADiskCopyDescription(disk);
    CFStringRef vol = desc ? CFDictionaryGetValue(desc, kDADiskDescriptionVolumeNameKey) : NULL;
    truncate_stdout_if_large();
    printf("prevented eject of %s", disk_name);
    log_msg(" (volume: %s)\n", vol);
    if (desc) CFRelease(desc);
    if (!g_no_mount)
        start_mount_retries(disk);
    return DADissenterCreate(kCFAllocatorDefault, kDAReturnNotPermitted,
                             CFSTR("SidePulse Pro Eject Prevention: keeping SD card attached"));
}

int main(int argc, char **argv) {
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-n") || !strcmp(argv[i], "--no-mount")) {
            g_no_mount = true;
        } else {
            fprintf(stderr, "usage: %s [-n | --no-mount]\n", argv[0]);
            return 2;
        }
    }
    g_da_pid = disk_arbitration_pid();
    g_session = DASessionCreate(kCFAllocatorDefault);
    if (!g_session) {
        fprintf(stderr, "DASessionCreate failed\n");
        return 1;
    }
    DARegisterDiskEjectApprovalCallback(g_session, NULL, eject_approval, NULL);
    DASessionScheduleWithRunLoop(g_session, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
    check_disk_arbitration(NULL, NULL);
    CFRunLoopTimerRef daemon_timer = CFRunLoopTimerCreate(
        kCFAllocatorDefault, CFAbsoluteTimeGetCurrent() + DA_PID_CHECK_SECONDS,
        DA_PID_CHECK_SECONDS, 0, 0, check_disk_arbitration, NULL);
    CFRunLoopAddTimer(CFRunLoopGetCurrent(), daemon_timer, kCFRunLoopDefaultMode);
    CFRelease(daemon_timer);
    printf("SidePulse Pro Eject Prevention: watching Disk Arbitration PID %ld\n", g_da_pid);
    fflush(stdout);
    CFRunLoopRun();
    return 0;
}
