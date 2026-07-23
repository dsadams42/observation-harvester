#!/bin/zsh
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

echo "Observation Harvester"
echo "Working directory: $APP_DIR"
echo

PYTHON_BIN=""
if command -v python3.12 >/dev/null 2>&1; then
  PYTHON_BIN="python3.12"
elif command -v python3 >/dev/null 2>&1; then
  if python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
  then
    PYTHON_BIN="python3"
  fi
fi

if [ -z "$PYTHON_BIN" ]; then
  echo "Python 3.12 or newer is required."
  echo "Install it, then double-click this launcher again."
  read "?Press Enter to close."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating local virtual environment..."
  "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate

echo "Installing/updating local app dependencies..."
python -m pip install -e ".[app]"

if ! command -v codex >/dev/null 2>&1; then
  echo
  echo "Codex CLI was not found on PATH."
  echo "Install it with:"
  echo "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
  echo
  echo "Then authenticate Codex CLI and double-click this launcher again."
  read "?Press Enter to close."
  exit 1
fi

echo
echo "Starting Observation Harvester at http://127.0.0.1:8765"
echo "Keep this Terminal window open while using the app."
echo

python -m pdt_observer app --workspace "$APP_DIR"
