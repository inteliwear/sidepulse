#!/bin/sh
if command -v sidepulse >/dev/null 2>&1; then
    exec sidepulse herdr sync
fi
if [ -x "$HOME/.local/bin/sidepulse" ]; then
    exec "$HOME/.local/bin/sidepulse" herdr sync
fi
exec /usr/local/bin/sidepulse herdr sync
