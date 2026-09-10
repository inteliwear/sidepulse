#include <array>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <crt_externs.h>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <libproc.h>
#include <limits>
#include <string>
#include <string_view>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/sysctl.h>
#include <sys/un.h>
#include <unistd.h>

namespace {

class UniqueFd {
public:
    explicit UniqueFd(int value = -1) : value_(value) {}
    ~UniqueFd() { if (value_ >= 0) ::close(value_); }
    UniqueFd(const UniqueFd&) = delete;
    UniqueFd& operator=(const UniqueFd&) = delete;
    int get() const { return value_; }

private:
    int value_;
};

struct Arguments {
    std::string provider;
    std::string event;
    std::string log_path;
    std::string spool_path;
    std::string notify_socket;
};

bool take_value(int& index, int count, char** values, std::string& output) {
    if (++index >= count) return false;
    output = values[index];
    return true;
}

bool parse_arguments(int count, char** values, Arguments& args) {
    for (int index = 1; index < count; ++index) {
        const std::string_view flag(values[index]);
        if (flag == "--provider") {
            if (!take_value(index, count, values, args.provider)) return false;
        } else if (flag == "--event") {
            if (!take_value(index, count, values, args.event)) return false;
        } else if (flag == "--log") {
            if (!take_value(index, count, values, args.log_path)) return false;
        } else if (flag == "--spool") {
            if (!take_value(index, count, values, args.spool_path)) return false;
        } else if (flag == "--notify-socket") {
            if (!take_value(index, count, values, args.notify_socket)) return false;
        } else {
            return false;
        }
    }
    if (args.provider.empty() || args.log_path.empty()) return false;
    if (args.spool_path.empty()) args.spool_path = args.log_path + ".spool";
    return true;
}

bool read_stdin(std::string& payload) {
    std::array<char, 16 * 1024> buffer{};
    for (;;) {
        const ssize_t count = ::read(STDIN_FILENO, buffer.data(), buffer.size());
        if (count > 0) {
            payload.append(buffer.data(), static_cast<std::size_t>(count));
        } else if (count == 0) {
            return true;
        } else if (errno != EINTR) {
            return false;
        }
    }
}

std::uint64_t nanoseconds(clockid_t clock) {
    timespec value{};
    if (::clock_gettime(clock, &value) != 0) return 0;
    return static_cast<std::uint64_t>(value.tv_sec) * 1'000'000'000ULL
        + static_cast<std::uint64_t>(value.tv_nsec);
}

void append_u32(std::string& output, std::uint32_t value) {
    for (int shift = 24; shift >= 0; shift -= 8) {
        output.push_back(static_cast<char>((value >> shift) & 0xff));
    }
}

void append_u64(std::string& output, std::uint64_t value) {
    for (int shift = 56; shift >= 0; shift -= 8) {
        output.push_back(static_cast<char>((value >> shift) & 0xff));
    }
}

void append_context_entry(std::string& context, std::string_view key,
                          std::string_view value) {
    append_u32(context, static_cast<std::uint32_t>(key.size()));
    append_u32(context, static_cast<std::uint32_t>(value.size()));
    context.append(key);
    context.append(value);
}

void capture_process_arguments(pid_t pid, std::string_view prefix, std::string& context) {
    int query[] = {CTL_KERN, KERN_PROCARGS2, pid};
    std::size_t size = 0;
    if (::sysctl(query, 3, nullptr, &size, nullptr, 0) != 0 || size < sizeof(int)
        || size > 1024 * 1024) {
        return;
    }
    std::string buffer(size, '\0');
    if (::sysctl(query, 3, buffer.data(), &size, nullptr, 0) != 0
        || size < sizeof(int)) {
        return;
    }
    buffer.resize(size);
    int argument_count = 0;
    std::memcpy(&argument_count, buffer.data(), sizeof(argument_count));
    if (argument_count <= 0) return;

    std::size_t cursor = sizeof(argument_count);
    while (cursor < buffer.size() && buffer[cursor] != '\0') ++cursor;
    while (cursor < buffer.size() && buffer[cursor] == '\0') ++cursor;
    for (int index = 0; index < argument_count && cursor < buffer.size(); ++index) {
        const std::size_t start = cursor;
        while (cursor < buffer.size() && buffer[cursor] != '\0') ++cursor;
        append_context_entry(context,
                             std::string(prefix) + "arg:" + std::to_string(index),
                             std::string_view(buffer.data() + start, cursor - start));
        while (cursor < buffer.size() && buffer[cursor] == '\0') ++cursor;
    }
}

std::string capture_context() {
    std::string context;
    constexpr std::array keys = {
        "SIDEPULSE_AGENT_ORIGIN",
        "SIDEPULSE_AGENT_ORIGIN_KIND",
        "SIDEPULSE_DISABLE_EVENT_SOCKET",
        "TERM_PROGRAM",
        "__CFBundleIdentifier",
        "HOME",
        "XDG_STATE_HOME",
    };
    for (const char* key : keys) {
        if (const char* value = ::getenv(key); value != nullptr) {
            append_context_entry(context, std::string("env:") + key, value);
        }
    }

    char*** environment = ::_NSGetEnviron();
    for (char** item = environment == nullptr ? nullptr : *environment;
         item != nullptr && *item != nullptr; ++item) {
        if (std::string_view(*item).starts_with("VSCODE_")) {
            append_context_entry(context, "env:VSCODE_PRESENT", "1");
            break;
        }
    }

    pid_t pid = ::getppid();
    for (int depth = 0; depth < 10 && pid > 1; ++depth) {
        proc_bsdinfo info{};
        if (::proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info))
            != sizeof(info)) {
            break;
        }
        const std::string prefix = "process:" + std::to_string(depth) + ":";
        append_context_entry(context, prefix + "pid", std::to_string(pid));
        append_context_entry(context, prefix + "ppid", std::to_string(info.pbi_ppid));
        append_context_entry(context, prefix + "comm", info.pbi_comm);
        std::array<char, PROC_PIDPATHINFO_MAXSIZE> path{};
        const int path_size = ::proc_pidpath(pid, path.data(), path.size());
        if (path_size > 0) {
            append_context_entry(context, prefix + "path",
                                 std::string_view(path.data(), path_size));
        }
        capture_process_arguments(pid, prefix, context);
        pid = static_cast<pid_t>(info.pbi_ppid);
    }
    return context;
}

bool append_size(std::string& output, std::size_t value) {
    if (value > std::numeric_limits<std::uint32_t>::max()) return false;
    append_u32(output, static_cast<std::uint32_t>(value));
    return true;
}

bool build_frame(const Arguments& args, const std::string& context,
                 const std::string& payload, std::string& frame) {
    constexpr std::uint32_t fixed_body_size = 36;
    const std::size_t variable_size = args.provider.size() + args.event.size()
        + args.log_path.size() + context.size() + payload.size();
    if (variable_size > std::numeric_limits<std::uint32_t>::max() - fixed_body_size) {
        return false;
    }

    frame.reserve(8 + fixed_body_size + variable_size);
    frame.append("SPH1", 4);
    append_u32(frame, fixed_body_size + static_cast<std::uint32_t>(variable_size));
    append_u64(frame, nanoseconds(CLOCK_REALTIME));
    append_u64(frame, nanoseconds(CLOCK_MONOTONIC));
    if (!append_size(frame, args.provider.size()) || !append_size(frame, args.event.size())
        || !append_size(frame, args.log_path.size()) || !append_size(frame, context.size())
        || !append_size(frame, payload.size())) {
        return false;
    }
    frame.append(args.provider);
    frame.append(args.event);
    frame.append(args.log_path);
    frame.append(context);
    frame.append(payload);
    return true;
}

bool write_all(int descriptor, const std::string& data) {
    std::size_t offset = 0;
    while (offset < data.size()) {
        const ssize_t count = ::write(descriptor, data.data() + offset, data.size() - offset);
        if (count > 0) {
            offset += static_cast<std::size_t>(count);
        } else if (count < 0 && errno == EINTR) {
            continue;
        } else {
            return false;
        }
    }
    return true;
}

bool append_frame(const std::string& path, const std::string& frame) {
    UniqueFd file(::open(path.c_str(), O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600));
    if (file.get() < 0 || ::flock(file.get(), LOCK_EX) != 0) return false;
    const off_t original_size = ::lseek(file.get(), 0, SEEK_END);
    const bool written = write_all(file.get(), frame);
    if (!written && original_size >= 0) (void)::ftruncate(file.get(), original_size);
    (void)::flock(file.get(), LOCK_UN);
    return written;
}

void notify(const std::string& path) {
    if (path.empty() || path.size() >= sizeof(sockaddr_un::sun_path)) return;
    UniqueFd socket(::socket(AF_UNIX, SOCK_DGRAM, 0));
    if (socket.get() < 0) return;
    const int flags = ::fcntl(socket.get(), F_GETFL, 0);
    if (flags >= 0) (void)::fcntl(socket.get(), F_SETFL, flags | O_NONBLOCK);

    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    std::memcpy(address.sun_path, path.c_str(), path.size() + 1);
    constexpr char wake = 1;
    (void)::sendto(socket.get(), &wake, 1, 0,
                   reinterpret_cast<const sockaddr*>(&address), sizeof(address));
}

}  // namespace

int main(int argc, char** argv) {
    Arguments args;
    std::string context;
    std::string payload;
    std::string frame;
    if (!parse_arguments(argc, argv, args) || !read_stdin(payload)) return 0;
    context = capture_context();
    if (!build_frame(args, context, payload, frame) || !append_frame(args.spool_path, frame)) {
        return 0;
    }
    notify(args.notify_socket);
    return 0;
}
