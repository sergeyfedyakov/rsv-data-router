#!/usr/bin/env bash
# rsv-data-router: local start (127.0.0.1:8780 by default)
# Works once "pip install -e ." has been run; PYTHONPATH also covers a
# not-yet-installed checkout. bin/RSVData.cfe (from the RSVData project)
# is optional: the flag is passed only when the file exists.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -x "$DIR/.venv/bin/python" ]; then
    PY="$DIR/.venv/bin/python"
elif [ -x "$DIR/.venv/Scripts/python.exe" ]; then
    PY="$DIR/.venv/Scripts/python.exe"
else
    PY="$(command -v python3 || command -v python)"
fi
RSVDATA_ARGS=()
if [ -f "$DIR/bin/RSVData.cfe" ]; then
    RSVDATA_ARGS=(--rsvdatabinary "$DIR/bin/RSVData.cfe")
fi
PYTHONPATH="$DIR/src${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m rsv_data_router \
    "${RSVDATA_ARGS[@]}" "$@"
