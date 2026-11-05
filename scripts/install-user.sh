#!/bin/sh
set -eu

PYTHON_BIN=${PYTHON_BIN:-python3}
INSTALL_ROOT=${SIDEPULSE_INSTALL_ROOT:-"$HOME/.local/share/sidepulse"}
BIN_DIR=${SIDEPULSE_BIN_DIR:-"$HOME/.local/bin"}
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV_DIR="$INSTALL_ROOT/venv"

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install "$SOURCE_DIR"

mkdir -p "$BIN_DIR"
ln -sf "$VENV_DIR/bin/sidepulse" "$BIN_DIR/sidepulse"
ln -sf "$VENV_DIR/bin/sidepulse" "$BIN_DIR/sp"
# Remove only aliases owned by this installation, including broken symlinks.
for legacy in agent-monitor agent-status-bar; do
    if [ -L "$BIN_DIR/$legacy" ] && [ "$(readlink "$BIN_DIR/$legacy")" = "$VENV_DIR/bin/$legacy" ]; then
        rm "$BIN_DIR/$legacy"
    fi
done

printf '%s\n' "SidePulse installed in $VENV_DIR"
printf '%s\n' "CLI linked at $BIN_DIR/sidepulse"
printf '%s\n' "Run: $BIN_DIR/sidepulse setup"
