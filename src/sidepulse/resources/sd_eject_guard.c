// SidePulse Pro Eject Prevention - veto software ejects of cards in the built-in SD reader.
//
// macOS loginwindow dissents the mount and ejects any disk that appears
// while the screen is locked (e.g. the SD slot re-enumerating after a
// hibernate wake). The eject goes through the same DiskArbitration approval
// mechanism, so a registered client can dissent it right back. This tool does
// that, then retries the mount every few seconds; the retries are dissented
// while locked and succeed after unlock. Retries stop once the disk mounts,
// when it disappears, or after MOUNT_RETRY_MAX_ATTEMPTS.
//
// Build: clang -o "SidePulse Pro Eject Prevention" sd_eject_guard.c \
//          -framework DiskArbitration -framework CoreFoundation
// Run:   ./sd_eject_guard [-n]   (leave running; Ctrl-C to stop)
//        -n / --no-mount: only veto ejects; don't retry mounting.

#include <CoreFoundation/CoreFoundation.h>
#include <DiskArbitration/DiskArbitration.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define MOUNT_RETRY_SECONDS 5.0
#define MOUNT_RETRY_MAX_ATTEMPTS 240
#define MAX_TRACKED_DISKS 8
#define LOG_TRUNCATE_BYTES (10 * 1024 * 1024)

static DASessionRef g_session;
static bool g_no_mount = false;

// One retry timer per disk; a repeat eject must not stack another.
typedef struct {
    char bsd[32];
    DADiskRef disk;
    CFRunLoopTimerRef timer;
    int attempts;
} MountRetry;

static MountRetry g_retries[MAX_TRACKED_DISKS];

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

static MountRetry *find_retry(const char *bsd) {
    for (int i = 0; i < MAX_TRACKED_DISKS; i++)
        if (g_retries[i].timer && strcmp(g_retries[i].bsd, bsd) == 0)
            return &g_retries[i];
    return NULL;
}

static void stop_retry(MountRetry *r) {
    if (!r || !r->timer) return;
    CFRunLoopTimerInvalidate(r->timer);
    CFRelease(r->timer);
    CFRelease(r->disk);
    memset(r, 0, sizeof(*r));
}

static void mount_done(DADiskRef disk, DADissenterRef dissenter, void *ctx) {
    (void)disk;
    (void)dissenter;
    (void)ctx;
}

static void retry_mount(CFRunLoopTimerRef timer, void *info) {
    (void)timer;
    MountRetry *r = (MountRetry *)info;
    if (is_mounted(r->disk) || ++r->attempts > MOUNT_RETRY_MAX_ATTEMPTS) {
        stop_retry(r);
        return;
    }
    DADiskMount(r->disk, NULL, kDADiskMountOptionDefault, mount_done, NULL);
}

static void start_mount_retries(DADiskRef disk, const char *bsd) {
    if (find_retry(bsd)) return;

    MountRetry *slot = NULL;
    for (int i = 0; i < MAX_TRACKED_DISKS; i++)
        if (!g_retries[i].timer) { slot = &g_retries[i]; break; }
    if (!slot) return;

    strlcpy(slot->bsd, bsd, sizeof(slot->bsd));
    slot->disk = (DADiskRef)CFRetain(disk);
    slot->attempts = 0;

    CFRunLoopTimerContext tctx = { 0, slot, NULL, NULL, NULL };
    slot->timer = CFRunLoopTimerCreate(
        kCFAllocatorDefault, CFAbsoluteTimeGetCurrent() + MOUNT_RETRY_SECONDS,
        MOUNT_RETRY_SECONDS, 0, 0, retry_mount, &tctx);
    if (!slot->timer) {
        CFRelease(slot->disk);
        memset(slot, 0, sizeof(*slot));
        return;
    }
    CFRunLoopAddTimer(CFRunLoopGetCurrent(), slot->timer, kCFRunLoopDefaultMode);
}

static void disk_disappeared(DADiskRef disk, void *ctx) {
    (void)ctx;
    const char *bsd = DADiskGetBSDName(disk);
    if (bsd) stop_retry(find_retry(bsd));
}

static DADissenterRef eject_approval(DADiskRef disk, void *ctx) {
    (void)ctx;
    if (!is_builtin_sd(disk))
        return NULL;  // not ours, allow
    const char *bsd = DADiskGetBSDName(disk);
    CFDictionaryRef desc = DADiskCopyDescription(disk);
    CFStringRef vol = desc ? CFDictionaryGetValue(desc, kDADiskDescriptionVolumeNameKey) : NULL;
    truncate_stdout_if_large();
    printf("prevented eject of %s", bsd ? bsd : "?");
    log_msg(" (volume: %s)\n", vol);
    if (desc) CFRelease(desc);
    if (!g_no_mount && bsd)
        start_mount_retries(disk, bsd);
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
    g_session = DASessionCreate(kCFAllocatorDefault);
    if (!g_session) {
        fprintf(stderr, "DASessionCreate failed\n");
        return 1;
    }
    DARegisterDiskEjectApprovalCallback(g_session, NULL, eject_approval, NULL);
    DARegisterDiskDisappearedCallback(g_session, NULL, disk_disappeared, NULL);
    DASessionScheduleWithRunLoop(g_session, CFRunLoopGetCurrent(), kCFRunLoopDefaultMode);
    CFRunLoopRun();
    return 0;
}
