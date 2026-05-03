#!/usr/bin/env bash
# One-time setup: venv, deps, browser, db migrate.
set -euo pipefail

cd "$(dirname "$0")"

PY=${PYTHON:-python3}

echo "==> Creating venv (.venv)"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Upgrading pip"
pip install -q --upgrade pip

echo "==> Installing requirements"
pip install -q -r requirements.txt

echo "==> Installing Playwright Firefox browser"
playwright install firefox

echo "==> Checking Poppler (for pdf2image)"
if ! command -v pdftoppm >/dev/null 2>&1; then
  echo "    Poppler not found. Install with: brew install poppler"
fi

echo "==> Checking Claude CLI"
if ! command -v claude >/dev/null 2>&1; then
  echo "    Claude CLI not found. Install Claude Code (claude.ai/code)."
fi

echo "==> Django migrate"
python manage.py migrate --no-input >/dev/null

echo
echo "Setup complete."
echo "Next:"
echo "  ./start.sh                  # launch UI on http://127.0.0.1:8000"
echo "  ./start.sh prep statistics  # run full pipeline for a class"
