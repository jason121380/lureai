#!/bin/zsh
set -e
cd "${0:A:h}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" run.py --reindex-only
"$PYTHON_BIN" run.py --host "${APP_HOST:-127.0.0.1}" --port "${APP_PORT:-8765}"
