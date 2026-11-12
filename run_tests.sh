#!/usr/bin/env bash
# Run the test suite in an isolated virtualenv.
# The app itself only needs fastapi + uvicorn (requirements.txt); httpx is pulled
# in here because fastapi.testclient requires it, keeping runtime deps pinned.
set -euo pipefail
cd "$(dirname "$0")"

VENV=${VENV:-.venv}
if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 "$VENV" >/dev/null 2>&1 || uv venv "$VENV" >/dev/null 2>&1
  uv pip install --python "$VENV/bin/python" -q -r requirements.txt "httpx==0.27.2" "pytest==8.3.3"
else
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r requirements.txt "httpx==0.27.2" "pytest==8.3.3"
fi

"$VENV/bin/pytest" -q "$@"
