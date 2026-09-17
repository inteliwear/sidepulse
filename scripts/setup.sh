#!/bin/sh
set -eu

PYTHON_BIN=${PYTHON_BIN:-python3}
INSTALL_SPEC=${SIDEPULSE_INSTALL_SPEC:-"git+https://github.com/inteliwear/sidepulse.git"}
VENV=${SIDEPULSE_INSTALL_ROOT:-"$HOME/.local/share/sidepulse"}/venv
BIN_DIR=${SIDEPULSE_BIN_DIR:-"$HOME/.local/bin"}

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    printf 'SidePulse requires Python 3.10 or newer. Could not find %s.\n' "$PYTHON_BIN" >&2
    exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    printf 'SidePulse requires Python 3.10 or newer.\n' >&2
    exit 1
fi

# Reuse a working environment on updates; recreating it can change the Python
# interpreter underneath an existing installation.
if [ ! -x "$VENV/bin/python" ]; then
    if ! "$PYTHON_BIN" -m venv "$VENV"; then
        printf 'Could not create a Python virtual environment.\n' >&2
        printf 'On Debian or Ubuntu, install python3-venv and run this command again.\n' >&2
        exit 1
    fi
fi

WHEEL_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sidepulse-setup.XXXXXX")
SERVICE_STOPPED=0
STATUS_BAR_STOPPED=0
SETUP_COMPLETE=0
STATUS_BAR_PLIST="$HOME/Library/LaunchAgents/io.sidepulse.agentstatus.plist"
LAUNCH_DOMAIN="gui/$(id -u)"

cleanup() {
    result=$?
    trap - EXIT
    if [ "$SETUP_COMPLETE" -eq 0 ]; then
        if [ "$SERVICE_STOPPED" -eq 1 ]; then
            "$VENV/bin/sidepulse" service start || printf 'Could not restart the background service.\n' >&2
        fi
        if [ "$STATUS_BAR_STOPPED" -eq 1 ]; then
            if ! launchctl print "$LAUNCH_DOMAIN/io.sidepulse.agentstatus" >/dev/null 2>&1; then
                launchctl bootstrap "$LAUNCH_DOMAIN" "$STATUS_BAR_PLIST" || printf 'Could not restart the menu-bar app.\n' >&2
            fi
        fi
    fi
    rm -rf "$WHEEL_DIR"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Fetch and build everything before pausing services. GitHub/network/build
# failures leave the installed code and running services alone.
"$VENV/bin/python" -m pip wheel --wheel-dir "$WHEEL_DIR" "$INSTALL_SPEC"
set -- "$WHEEL_DIR"/sidepulse-*.whl
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    printf 'The build did not produce exactly one SidePulse wheel.\n' >&2
    exit 1
fi
SIDEPULSE_WHEEL=$1

if [ -x "$VENV/bin/sidepulse" ] && "$VENV/bin/sidepulse" service status >/dev/null 2>&1; then
    "$VENV/bin/sidepulse" service stop
    SERVICE_STOPPED=1
fi
if [ "$(uname -s)" = "Darwin" ] && [ -f "$STATUS_BAR_PLIST" ]; then
    if launchctl print "$LAUNCH_DOMAIN/io.sidepulse.agentstatus" >/dev/null 2>&1; then
        launchctl bootout "$LAUNCH_DOMAIN" "$STATUS_BAR_PLIST"
        STATUS_BAR_STOPPED=1
    fi
fi

# --upgrade alone can retain old code when GitHub commits share a version.
# Force replacement, using only the wheels already downloaded above.
"$VENV/bin/python" -m pip install --upgrade --force-reinstall \
    --no-index --find-links "$WHEEL_DIR" "$SIDEPULSE_WHEEL"

mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/sidepulse" "$BIN_DIR/sidepulse"
ln -sf "$VENV/bin/agent-monitor" "$BIN_DIR/agent-monitor"

"$VENV/bin/sidepulse" setup
SETUP_COMPLETE=1
"$VENV/bin/sidepulse" --version

printf '\nSidePulse is installed.\n'
printf 'Command: %s\n' "$BIN_DIR/sidepulse"
case ":${PATH:-}:" in
    *":$BIN_DIR:"*) ;;
    *) printf 'Add %s to PATH to run sidepulse directly.\n' "$BIN_DIR" ;;
esac
