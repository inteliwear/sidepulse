#!/usr/bin/env bash
set -u -o pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/macos-sd-diagnostics.sh [options]

Collect macOS host-side diagnostics for the built-in SD reader/card path.

Options:
  --last DURATION     Log window passed to `log show --last` (default: 30m).
                      Examples: 10m, 2h, 1d.
  --start DATE        Start time for `log show --start`.
                      Example: "2026-06-25 00:40:00".
  --end DATE          End time for `log show --end`.
                      Use with --start. If omitted, log show uses "now".
  --out PATH          Output report path.
  --cmd-timeout SEC   Timeout for storage/registry commands (default: 20).
  --log-timeout SEC   Timeout for each `log show` query (default: 180).
  -h, --help          Show this help.

Examples:
  scripts/macos-sd-diagnostics.sh --last 2h
  scripts/macos-sd-diagnostics.sh --start "2026-06-25 00:40:00" --end "2026-06-25 00:50:00"
USAGE
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stamp="$(date '+%Y%m%d-%H%M%S')"
last_window="${SD_DIAG_LAST:-30m}"
start_time=""
end_time=""
out_path="$repo_root/temp_data/macos-sd-diagnostics-$stamp.txt"
cmd_timeout="${SD_DIAG_CMD_TIMEOUT:-20}"
log_timeout="${SD_DIAG_LOG_TIMEOUT:-180}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --last)
      [[ $# -ge 2 ]] || { echo "--last requires a duration" >&2; exit 2; }
      last_window="$2"
      shift 2
      ;;
    --start)
      [[ $# -ge 2 ]] || { echo "--start requires a date" >&2; exit 2; }
      start_time="$2"
      shift 2
      ;;
    --end)
      [[ $# -ge 2 ]] || { echo "--end requires a date" >&2; exit 2; }
      end_time="$2"
      shift 2
      ;;
    --out)
      [[ $# -ge 2 ]] || { echo "--out requires a path" >&2; exit 2; }
      out_path="$2"
      shift 2
      ;;
    --cmd-timeout)
      [[ $# -ge 2 ]] || { echo "--cmd-timeout requires seconds" >&2; exit 2; }
      cmd_timeout="$2"
      shift 2
      ;;
    --log-timeout)
      [[ $# -ge 2 ]] || { echo "--log-timeout requires seconds" >&2; exit 2; }
      log_timeout="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

mkdir -p "$(dirname "$out_path")"

section() {
  printf '\n\n===== %s =====\n\n' "$1"
}

run() {
  printf '$'
  printf ' %q' "$@"
  printf '\n'
  "$@"
  local status=$?
  if [[ $status -ne 0 ]]; then
    printf '[exit status %d]\n' "$status"
  fi
}

run_shell() {
  printf '$ %s\n' "$*"
  /bin/bash -lc "$*"
  local status=$?
  if [[ $status -ne 0 ]]; then
    printf '[exit status %d]\n' "$status"
  fi
}

run_timeout() {
  local timeout_seconds="$1"
  shift
  printf '$ timeout %ss' "$timeout_seconds"
  printf ' %q' "$@"
  printf '\n'
  perl -e '
    use strict;
    use warnings;

    my $timeout = shift @ARGV;
    my $pid = fork();
    die "fork: $!\n" unless defined $pid;

    if ($pid == 0) {
      setpgrp(0, 0);
      exec @ARGV or die "exec @ARGV: $!\n";
    }

    local $SIG{ALRM} = sub {
      kill "TERM", -$pid;
      select undef, undef, undef, 1;
      kill "KILL", -$pid;
      print STDERR "\n[timeout after ${timeout}s]\n";
      exit 124;
    };

    alarm $timeout;
    waitpid($pid, 0);
    my $status = $?;
    alarm 0;

    if ($status == -1) {
      exit 127;
    } elsif ($status & 127) {
      exit 128 + ($status & 127);
    } else {
      exit($status >> 8);
    }
  ' "$timeout_seconds" "$@"
  local status=$?
  if [[ $status -ne 0 ]]; then
    printf '[exit status %d]\n' "$status"
  fi
}

run_timeout_shell() {
  local timeout_seconds="$1"
  shift
  run_timeout "$timeout_seconds" /bin/bash -lc "$*"
}

log_window_args=()
if [[ -n "$start_time" ]]; then
  log_window_args=(--start "$start_time")
  if [[ -n "$end_time" ]]; then
    log_window_args+=(--end "$end_time")
  fi
else
  log_window_args=(--last "$last_window")
fi

kernel_predicate='process == "kernel" && (eventMessage CONTAINS[c] "AppleSDXC" || eventMessage CONTAINS[c] "Port-SD" || eventMessage CONTAINS[c] "SDXC" || eventMessage CONTAINS[c] "SD Card" || eventMessage CONTAINS[c] "IOBlockStorage" || eventMessage CONTAINS[c] "IOMedia")'
disk_predicate='(process == "diskarbitrationd" || process == "diskmanagementd" || process == "kernel") && (eventMessage CONTAINS[c] "disk" || eventMessage CONTAINS[c] "SidePulse" || eventMessage CONTAINS[c] "Side" || eventMessage CONTAINS[c] "FAT" || eventMessage CONTAINS[c] "MS-DOS" || eventMessage CONTAINS[c] "IOMedia" || eventMessage CONTAINS[c] "eject" || eventMessage CONTAINS[c] "mount")'

{
  section "Run Metadata"
  printf 'Generated: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
  printf 'Host: %s\n' "$(hostname)"
  printf 'Working directory: %s\n' "$(pwd)"
  printf 'Repository root: %s\n' "$repo_root"
  printf 'Output path: %s\n' "$out_path"
  printf 'Command timeout: %ss\n' "$cmd_timeout"
  printf 'Log timeout: %ss\n' "$log_timeout"
  if [[ -n "$start_time" ]]; then
    printf 'Log window: start=%s end=%s\n' "$start_time" "${end_time:-now}"
  else
    printf 'Log window: last=%s\n' "$last_window"
  fi

  section "System"
  run date
  run sw_vers
  run uname -a
  run uptime

  section "Card Reader Summary"
  run_timeout "$cmd_timeout" system_profiler SPCardReaderDataType
  run_timeout "$cmd_timeout" system_profiler -json SPCardReaderDataType

  section "Disk State"
  run_timeout "$cmd_timeout" diskutil list
  run_timeout "$cmd_timeout" diskutil info -all
  run_timeout "$cmd_timeout" mount
  run_timeout "$cmd_timeout" df -h
  run_timeout_shell "$cmd_timeout" 'ls -la /Volumes'

  section "AppleSDXC IORegistry Path"
  run_timeout "$cmd_timeout" ioreg -p IOService -w0 -t -r -c AppleSDXC

  section "AppleSDXC IORegistry Properties"
  run_timeout "$cmd_timeout" ioreg -p IOService -l -w0 -r -c AppleSDXC

  section "AppleSDXC Quick Filter"
  run_timeout_shell "$cmd_timeout" 'ioreg -p IOService -l -w0 -r -c AppleSDXC | grep -E "AppleSDXC|Port-SD|Card Present|ConnectionActive|TransportsActive|Authorization|Block Count|Ejected|IOMedia|BSD Name|Errors|Operations|Product Name|Manufacture|Serial|Card Type|Specification|Bus Width|Speed Mode|Clock|TRM|HashStatus|DriverStatus" || true'

  section "IOPort Plane SD View"
  run_timeout_shell "$cmd_timeout" 'for name in "Port-SD Card@1" "SD"; do echo "--- $name ---"; ioreg -p IOPort -l -w0 -r -n "$name"; done'

  section "IOMedia Filter"
  run_timeout_shell "$cmd_timeout" 'ioreg -p IOService -l -w0 -r -c IOMedia | grep -E "IOMedia|BSD Name|Content|Whole|Writable|Removable|Size|Leaf|Preferred Block Size|SidePulse|Side|PulseDot|FAT|MS-DOS|Secure Digital" || true'

  section "Power Assertions"
  run_timeout "$cmd_timeout" pmset -g assertions

  section "Kernel SD Logs"
  run_timeout "$log_timeout" log show --style syslog --info --debug "${log_window_args[@]}" --predicate "$kernel_predicate"

  section "Disk Arbitration And Mount Logs"
  run_timeout "$log_timeout" log show --style syslog --info --debug "${log_window_args[@]}" --predicate "$disk_predicate"
} >"$out_path" 2>&1

printf 'Wrote diagnostics to %s\n' "$out_path"
