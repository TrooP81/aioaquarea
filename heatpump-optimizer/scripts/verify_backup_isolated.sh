#!/bin/sh
# Explicitly approved maintenance only. No app imports, device calls or live target.
set -eu
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/restore_drill.py" "$@"
