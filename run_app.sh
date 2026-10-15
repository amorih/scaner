#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/run/user/1000/gdm/Xauthority}"
export QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu --no-sandbox"

if [ -f "$DIR/env/bin/python" ]; then
    exec "$DIR/env/bin/python" "$DIR/desktop_app.py" "$@"
else
    exec python3 "$DIR/desktop_app.py" "$@"
fi
