#!/bin/sh
set -eu

PYTHON_BIN=${PYTHON_BIN:-python3}
INSTALL_SPEC=${SIDEPULSE_INSTALL_SPEC:-"git+https://github.com/inteliwear/sidepulse.git"}
INSTALL_ROOT=${SIDEPULSE_INSTALL_ROOT:-"$HOME/.local/share/sidepulse"}
VENV=$INSTALL_ROOT/venv
BIN_DIR=${SIDEPULSE_BIN_DIR:-"$HOME/.local/bin"}

# Check before Python discovery: Apple's python3/clang stubs can otherwise open
# an installation dialog, and a late compiler failure leaves setup incomplete.
if [ "$(uname -s)" = Darwin ]; then
    if ! xcode-select -p >/dev/null 2>&1 || ! xcrun --find clang >/dev/null 2>&1; then
        cat >&2 <<'EOF'
SidePulse setup needs Apple Command Line Tools to build its eject-prevention helper.

1. Run: xcode-select --install
2. Complete Apple's installer and wait for installation to finish.
3. Run this SidePulse installer again.

If the tools are already installed, select them with:
  sudo xcode-select --switch /Library/Developer/CommandLineTools
For a full Xcode installation, select its Contents/Developer directory instead.
EOF
        exit 1
    fi
fi

usable_python() {
    command -v "$1" >/dev/null 2>&1 &&
        "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1
}

install_macos_python() (
    # Keep uv and its Python private to SidePulse. The unmanaged installer does
    # not edit shell profiles, and --no-bin avoids global Python symlinks.
    uv_bin=$INSTALL_ROOT/uv/uv
    if [ ! -x "$uv_bin" ]; then
        bootstrap_dir=$(mktemp -d "${TMPDIR:-/tmp}/sidepulse-python.XXXXXX")
        trap 'rm -rf "$bootstrap_dir"' EXIT
        curl -fsSL https://astral.sh/uv/install.sh -o "$bootstrap_dir/install-uv.sh" || exit 1
        UV_UNMANAGED_INSTALL="$INSTALL_ROOT/uv" sh "$bootstrap_dir/install-uv.sh" >&2 || exit 1
    fi
    printf 'Installing Python 3.13 for SidePulse…\n' >&2
    UV_PYTHON_INSTALL_DIR="$INSTALL_ROOT/python" "$uv_bin" python install \
        --no-config --no-bin 3.13 >&2 || exit 1
    UV_PYTHON_INSTALL_DIR="$INSTALL_ROOT/python" "$uv_bin" python find \
        --no-config --no-project --managed-python 3.13
)

# Updates use the existing interpreter even if the shell's python3 is older.
if [ -x "$VENV/bin/python" ]; then
    if ! usable_python "$VENV/bin/python"; then
        printf 'The existing SidePulse environment needs Python 3.10 or newer: %s\n' "$VENV" >&2
        exit 1
    fi
    PYTHON_BIN=$VENV/bin/python
elif ! usable_python "$PYTHON_BIN"; then
    found_python=
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
        if usable_python "$candidate"; then
            found_python=$(command -v "$candidate")
            break
        fi
    done
    if [ -n "$found_python" ]; then
        PYTHON_BIN=$found_python
    elif [ "$(uname -s)" = "Darwin" ]; then
        if ! PYTHON_BIN=$(install_macos_python) || ! usable_python "$PYTHON_BIN"; then
            printf 'Could not install Python for SidePulse. Check your internet connection and try again.\n' >&2
            exit 1
        fi
    else
        printf 'SidePulse requires Python 3.10 or newer. Install it and run setup again.\n' >&2
        exit 1
    fi
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
ln -sf "$VENV/bin/sidepulse" "$BIN_DIR/sp"
# Remove only aliases owned by this installation, including broken symlinks.
for legacy in agent-monitor agent-status-bar; do
    if [ -L "$BIN_DIR/$legacy" ] && [ "$(readlink "$BIN_DIR/$legacy")" = "$VENV/bin/$legacy" ]; then
        rm "$BIN_DIR/$legacy"
    fi
done

"$VENV/bin/sidepulse" setup
SETUP_COMPLETE=1
"$VENV/bin/sidepulse" --version

printf '\nSidePulse is installed.\n'
printf 'Command: %s\n' "$BIN_DIR/sidepulse"
case ":${PATH:-}:" in
    *":$BIN_DIR:"*) ;;
    *) printf 'Add %s to PATH to run sidepulse directly.\n' "$BIN_DIR" ;;
esac
