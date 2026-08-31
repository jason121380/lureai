#!/bin/zsh
set -e
cd "${0:A:h}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" run.py --profile designer_coach --reindex-only
"$PYTHON_BIN" run.py --profile designer_coach --host "${APP_HOST:-127.0.0.1}" --port "${APP_PORT:-8766}"
