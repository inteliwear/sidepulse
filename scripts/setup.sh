#!/bin/sh
set -eu

VENV="$HOME/.local/share/sidepulse/venv"
BIN_DIR="$HOME/.local/bin"

python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade \
    "git+https://github.com/inteliwear/sidepulse.git"

mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/sidepulse" "$BIN_DIR/sidepulse"
ln -sf "$VENV/bin/agent-monitor" "$BIN_DIR/agent-monitor"

"$VENV/bin/sidepulse" setup

printf '\nSidePulse is installed.\n'
printf 'Command: %s\n' "$BIN_DIR/sidepulse"
