// SPDX-License-Identifier: MIT
// Bounded MagSafe 3 LED transport. Only the AppleSMC ACLC key is writable.
// Restoration design follows MagDot's charge-color seed then system release.
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <Security/Security.h>
#include <bsm/libbsm.h>
#include <launch.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

typedef struct { unsigned char major, minor, build, reserved; unsigned short release; } SMCVersion;
typedef struct { unsigned short version, length; unsigned int cpu, gpu, memory; } SMCLimits;
typedef struct { unsigned int size, type; unsigned char attributes; } SMCKey;
typedef struct {
    unsigned int key;
    SMCVersion version;
    SMCLimits limits;
    SMCKey info;
    unsigned char result, status, command;
    unsigned int index;
    unsigned char data[32];
} SMCMessage;
_Static_assert(sizeof(SMCMessage) == 80, "AppleSMC ABI");
static io_connect_t smc;
#ifdef SIDEPULSE_MAGSAFE_TEST
static int mock_led;
#endif
static volatile sig_atomic_t stopping;
static void stop_signal(int signal_number) { (void)signal_number; stopping = 1; }
static double seconds_now(void) {
    struct timespec value;
    clock_gettime(CLOCK_MONOTONIC, &value);
    return value.tv_sec + value.tv_nsec / 1e9;
}
static int open_smc(void) {
#ifdef SIDEPULSE_MAGSAFE_TEST
    return 0;
#endif
    io_service_t service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSMC"));
    if (!service) return -1;
    kern_return_t result = IOServiceOpen(service, mach_task_self(), 0, &smc);
    IOObjectRelease(service);
    return result == KERN_SUCCESS ? 0 : -1;
}
static int smc_call(SMCMessage *request) {
    SMCMessage response = {0};
    size_t length = sizeof(response);
    kern_return_t result = IOConnectCallStructMethod(smc, 2, request, sizeof(*request), &response, &length);
    if (result != KERN_SUCCESS || response.result || length != sizeof(response)) return -1;
    *request = response;
    return 0;
}
static int led_read(void) {
#ifdef SIDEPULSE_MAGSAFE_TEST
    return mock_led;
#endif
    SMCMessage request = {.key = 0x41434c43, .command = 9};
    if (smc_call(&request) || request.info.size != 1) return -1;
    request = (SMCMessage){.key = 0x41434c43, .command = 5, .info.size = 1};
    return smc_call(&request) ? -1 : request.data[0];
}
static int led_write(int value) {
#ifdef SIDEPULSE_MAGSAFE_TEST
    mock_led = value;
    FILE *trace = fopen(getenv("SIDEPULSE_MAGSAFE_TRACE"), "a");
    if (!trace) return -1;
    fprintf(trace, "%d\n", value);
    fclose(trace);
    return 0;
#endif
    SMCMessage request = {.key = 0x41434c43, .command = 6, .info.size = 1};
    request.data[0] = value;
    return smc_call(&request);
}
static int restore_charging_light(void) {
    int color = 4;
#ifndef SIDEPULSE_MAGSAFE_TEST
    io_service_t battery = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSmartBattery"));
    if (battery) {
        CFTypeRef charged = IORegistryEntryCreateCFProperty(battery, CFSTR("FullyCharged"), kCFAllocatorDefault, 0);
        if (charged) {
            if (CFGetTypeID(charged) == CFBooleanGetTypeID() && CFBooleanGetValue(charged)) color = 3;
            CFRelease(charged);
        }
        IOObjectRelease(battery);
    }
#endif
    int seeded = led_write(color);
    usleep(300000);
    int released = led_write(0);
    return seeded || released;
}
static int led_session(int client) {
    signal(SIGPIPE, SIG_IGN);
    // Share MagDot's ownership lock; never stop or take over its controller.
    const char *lock_path = "/var/run/magdot.lock";
#ifdef SIDEPULSE_MAGSAFE_TEST
    lock_path = getenv("SIDEPULSE_MAGSAFE_LOCK");
#endif
    int lock = open(lock_path, O_CREAT | O_RDWR | O_NOFOLLOW, 0600);
    if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB)) {
        dprintf(client, "MagDot or another controller owns the MagSafe LED. Stop it before adding this device.\n");
        return 1;
    }
    if (open_smc() || led_read() < 0) {
        dprintf(client, "MagSafe 3 LED control is unavailable on this Mac.\n");
        return 1;
    }
    int previous = led_read();
    if (previous != 0 && previous != 3 && previous != 4) {
        dprintf(client, "Another LED controller may be active; refusing takeover.\n");
        return 1;
    }
    int heartbeat[2];
    if (pipe(heartbeat)) return 1;
    pid_t guardian = fork();
    if (guardian < 0) return 1;
    if (guardian == 0) {
        // A service shutdown must let the guardian finish restoration after
        // the controller closes its pipe, even when launchd signals the group.
        signal(SIGTERM, SIG_IGN);
        signal(SIGINT, SIG_IGN);
        signal(SIGHUP, SIG_IGN);
        close(client);
        close(heartbeat[1]);
#ifndef SIDEPULSE_MAGSAFE_TEST
        IOServiceClose(smc);
#endif
        if (open_smc()) _exit(1);
        int active = 0;
        struct pollfd watch = {.fd = heartbeat[0], .events = POLLIN};
        char updates[128];
        while (poll(&watch, 1, 4000) > 0) {
            ssize_t count = read(heartbeat[0], updates, sizeof(updates));
            if (count <= 0) break;
            active = updates[count - 1] == '1';
        }
        int failed = active ? restore_charging_light() : 0;
#ifndef SIDEPULSE_MAGSAFE_TEST
        IOServiceClose(smc);
#endif
        _exit(failed ? 1 : 0);
    }
    close(heartbeat[0]);
    struct sigaction action = {.sa_handler = stop_signal};
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGHUP, &action, NULL);
    sigset_t signals;
    sigemptyset(&signals);
    sigprocmask(SIG_SETMASK, &signals, NULL);
    dprintf(client, "READY\n");
    char buffer[128] = {0};
    size_t used = 0;
    int state = 0, active = 0, failed = 0;
    double last_command = seconds_now(), started = last_command;
    struct pollfd input = {.fd = client, .events = POLLIN};
    while (!stopping && seconds_now() - last_command < 4) {
        int available = poll(&input, 1, 100);
        if (available < 0) { if (errno == EINTR) continue; failed = 1; break; }
        if (available > 0) {
            ssize_t count = read(client, buffer + used, sizeof(buffer) - used - 1);
            if (count <= 0) break;
            used += count;
            buffer[used] = 0;
            char *line = buffer, *newline;
            while ((newline = strchr(line, '\n'))) {
                *newline = 0;
                int next = !strcmp(line, "idle") ? 0 : !strcmp(line, "working") ? 1 :
                    !strcmp(line, "done") ? 2 : !strcmp(line, "ask") ? 3 : -1;
                if (!strcmp(line, "quit")) { stopping = 1; break; }
                if (next < 0) { failed = 1; stopping = 1; break; }
                if (state != next) { state = next; started = seconds_now(); }
                last_command = seconds_now();
                line = newline + 1;
            }
            used -= (size_t)(line - buffer);
            memmove(buffer, line, used);
            if (used == sizeof(buffer) - 1) { failed = 1; break; }
        }
        if (stopping) break;
        if (active && led_read() != previous) { failed = 1; break; }
        if (state == 0 && active) {
            if (restore_charging_light()) { failed = 1; break; }
            active = 0;
            previous = led_read();
        } else if (state != 0) {
            if (!active) {
                previous = led_read();
                if (previous != 0 && previous != 3 && previous != 4) { failed = 1; break; }
                // Arm restoration before taking control, including a crash
                // between this heartbeat and the first hardware write.
                if (write(heartbeat[1], "1", 1) != 1) { failed = 1; break; }
                active = 1;
            }
            int phase = ((int)((seconds_now() - started) / 0.8)) % 2;
            int value = state == 1 ? (phase ? 4 : 3) : phase ? 1 : state == 2 ? 3 : 4;
            if (value != previous) {
                if (led_write(value)) { failed = 1; break; }
                previous = value;
            }
        }
        if (write(heartbeat[1], active ? "1" : "0", 1) != 1) { failed = 1; break; }
    }
    close(heartbeat[1]);
    int status = 0;
    while (waitpid(guardian, &status, 0) < 0 && errno == EINTR) {}
#ifndef SIDEPULSE_MAGSAFE_TEST
    IOServiceClose(smc);
#endif
    dprintf(client, failed || status ? "MagSafe control ended; restoration attempted.\n" : "RESTORED\n");
    close(client);
    return failed || status ? 1 : 0;
}

#ifndef SIDEPULSE_MAGSAFE_TEST
#ifndef SIDEPULSE_APP_ID
#define SIDEPULSE_APP_ID "io.sidepulse.cli"
#endif
// Authenticate the live process using its audit token, bundle ID and the
// helper's signing team. Ad-hoc or unrelated clients cannot issue LED commands.
static int client_is_sidepulse(int client) {
    audit_token_t token;
    socklen_t size = sizeof(token);
    if (getsockopt(client, SOL_LOCAL, LOCAL_PEERTOKEN, &token, &size) || size != sizeof(token)) return 0;
    CFDataRef audit_data = CFDataCreate(NULL, (const UInt8 *)&token, sizeof(token));
    const void *attribute_keys[] = {kSecGuestAttributeAudit};
    const void *attribute_values[] = {audit_data};
    CFDictionaryRef attributes = CFDictionaryCreate(NULL, attribute_keys, attribute_values, 1,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    SecCodeRef peer = NULL;
    SecCodeRef own_code = NULL;
    CFDictionaryRef own_info = NULL;
    SecRequirementRef rule = NULL;
    int trusted = 0;
    if (SecCodeCopyGuestWithAttributes(NULL, attributes, kSecCSDefaultFlags, &peer) == errSecSuccess &&
        SecCodeCopySelf(kSecCSDefaultFlags, &own_code) == errSecSuccess &&
        SecCodeCopySigningInformation(own_code, kSecCSSigningInformation, &own_info) == errSecSuccess) {
        CFStringRef team = CFDictionaryGetValue(own_info, kSecCodeInfoTeamIdentifier);
        if (team) {
            CFStringRef expression = CFStringCreateWithFormat(NULL, NULL,
                CFSTR("anchor apple generic and identifier \"%s\" and certificate leaf[subject.OU] = \"%@\""),
                SIDEPULSE_APP_ID, team);
            if (SecRequirementCreateWithString(expression, kSecCSDefaultFlags, &rule) == errSecSuccess)
                trusted = SecCodeCheckValidity(peer, kSecCSDefaultFlags, rule) == errSecSuccess;
            CFRelease(expression);
        }
    }
    if (rule) CFRelease(rule);
    if (own_info) CFRelease(own_info);
    if (own_code) CFRelease(own_code);
    if (peer) CFRelease(peer);
    CFRelease(attributes);
    CFRelease(audit_data);
    return trusted;
}

static int daemon_main(void) {
    int *sockets = NULL;
    size_t count = 0;
    if (launch_activate_socket("MagSafe", &sockets, &count) || count != 1) {
        if (sockets) {
            for (size_t i = 0; i < count; i++) close(sockets[i]);
            free(sockets);
        }
        return 1;
    }
    int listener = sockets[0];
    free(sockets);
    struct pollfd input = {.fd = listener, .events = POLLIN};
    while (1) {
        int available = poll(&input, 1, 2000);
        if (available == 0) { close(listener); return 0; }
        if (available < 0) { if (errno == EINTR) continue; close(listener); return 1; }
        int client = accept(listener, NULL, NULL);
        if (client < 0) continue;
        if (!client_is_sidepulse(client)) { close(client); continue; }
        pid_t worker = fork();
        if (worker == 0) {
            close(listener);
            _exit(led_session(client));
        }
        close(client);
        int status = 0;
        if (worker > 0) while (waitpid(worker, &status, 0) < 0 && errno == EINTR) {}
        close(listener);
        return worker < 0 ? 1 : 0;
    }
}
#endif

int main(int argc, char **argv) {
#ifdef SIDEPULSE_MAGSAFE_TEST
    // Test-only reverse connection exercises restoration without root or SMC.
    if (argc != 3) return 64;
    char *end = NULL;
    unsigned long requested_uid = strtoul(argv[2], &end, 10);
    if (!*argv[2] || *end || requested_uid > 0xffffffffUL) return 64;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(argv[1]) >= sizeof(address.sun_path)) return 64;
    strlcpy(address.sun_path, argv[1], sizeof(address.sun_path));
    int client = socket(AF_UNIX, SOCK_STREAM, 0);
    if (client < 0 || connect(client, (struct sockaddr *)&address, sizeof(address))) return 1;
    uid_t uid;
    gid_t gid;
    if (getpeereid(client, &uid, &gid) || uid != (uid_t)requested_uid) return 1;
    return led_session(client);
#else
    if (geteuid() != 0 || argc != 2 || strcmp(argv[1], "--daemon")) return 64;
    return daemon_main();
#endif
}
