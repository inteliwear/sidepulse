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

if ! "$PYTHON_BIN" -m venv "$VENV"; then
    printf 'Could not create a Python virtual environment.\n' >&2
    printf 'On Debian or Ubuntu, install python3-venv and run this command again.\n' >&2
    exit 1
fi
"$VENV/bin/python" -m pip install --upgrade \
    "$INSTALL_SPEC"

mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/sidepulse" "$BIN_DIR/sidepulse"
ln -sf "$VENV/bin/agent-monitor" "$BIN_DIR/agent-monitor"

"$VENV/bin/sidepulse" setup

printf '\nSidePulse is installed.\n'
printf 'Command: %s\n' "$BIN_DIR/sidepulse"
case ":${PATH:-}:" in
    *":$BIN_DIR:"*) ;;
    *) printf 'Add %s to PATH to run sidepulse directly.\n' "$BIN_DIR" ;;
esac
