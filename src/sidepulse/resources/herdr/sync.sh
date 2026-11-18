#!/bin/sh
if command -v sidepulse >/dev/null 2>&1; then
    exec sidepulse herdr sync
fi
exec "$HOME/.local/bin/sidepulse" herdr sync
