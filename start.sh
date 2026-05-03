#!/usr/bin/env bash
# Multi-purpose launcher.
#
# Usage:
#   ./start.sh                              # launch Django UI on PORT (default 8000)
#   ./start.sh ui                           # same
#   ./start.sh prep <class-name>            # run full pipeline for a class
#   ./start.sh prep <class-name> --ask "Q"  # also fire a semantic query
#   ./start.sh auth                         # re-login to Canvas (fresh cookies)
#   ./start.sh panopto                      # try to scrape Panopto transcripts
#   ./start.sh stop                         # kill any running UI on PORT
#   ./start.sh open                         # open the UI in your browser
#   ./start.sh status                       # show whether UI is running
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
UI_PIDFILE=".ui.pid"

ensure_venv() {
  if [ ! -d .venv ]; then
    echo "First run: bootstrapping with ./setup.sh"
    ./setup.sh
  fi
}

activate() {
  ensure_venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
}

ui_running() {
  if [ -f "$UI_PIDFILE" ] && kill -0 "$(cat "$UI_PIDFILE")" 2>/dev/null; then
    return 0
  fi
  return 1
}

cmd_ui() {
  activate
  if ui_running; then
    echo "UI already running (PID $(cat "$UI_PIDFILE"))."
    cmd_open
    return 0
  fi
  : "${USE_BACKEND:=gemini}"
  : "${GCP_PROJECT:=nd-canvas-ocr-1777818991}"
  : "${GCP_LOCATIONS:=us-central1,us-east5,us-west4}"
  : "${GEMINI_MODEL:=gemini-2.5-pro}"
  export USE_BACKEND GCP_PROJECT GCP_LOCATIONS GEMINI_MODEL
  echo "Starting Django UI on http://127.0.0.1:$PORT (backend=$USE_BACKEND model=$GEMINI_MODEL)"
  nohup python manage.py runserver "$PORT" >/tmp/ndcanvas-ui.log 2>&1 &
  echo $! > "$UI_PIDFILE"
  sleep 2
  echo "PID $(cat "$UI_PIDFILE")  log: /tmp/ndcanvas-ui.log"
  cmd_open
}

cmd_stop() {
  if [ -f "$UI_PIDFILE" ]; then
    PID="$(cat "$UI_PIDFILE")"
    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID" && echo "Stopped PID $PID"
    fi
    rm -f "$UI_PIDFILE"
  fi
  pkill -f "manage.py runserver $PORT" 2>/dev/null || true
}

cmd_open() {
  case "$(uname)" in
    Darwin) open "http://127.0.0.1:$PORT/" ;;
    Linux)  xdg-open "http://127.0.0.1:$PORT/" 2>/dev/null || true ;;
    *)      echo "Visit http://127.0.0.1:$PORT/" ;;
  esac
}

cmd_status() {
  if ui_running; then
    echo "UI running on PID $(cat "$UI_PIDFILE") at http://127.0.0.1:$PORT/"
  else
    echo "UI not running."
  fi
}

cmd_prep() {
  activate
  if [ $# -lt 1 ]; then
    echo "Usage: $0 prep <class-name> [--skip-sync] [--ask 'Q'] [--with-summaries] [--with-panopto]"
    python prep.py --list
    return 1
  fi
  python prep.py "$@"
}

cmd_auth() {
  activate
  python auth.py
}

cmd_panopto() {
  activate
  COURSE_DIR="${1:-downloads/128781_statistics}"
  python panopto_browser.py --course-dir "$COURSE_DIR" "${@:2}"
}

case "${1:-ui}" in
  ui)      cmd_ui ;;
  stop)    cmd_stop ;;
  open)    cmd_open ;;
  status)  cmd_status ;;
  prep)    shift; cmd_prep "$@" ;;
  auth)    cmd_auth ;;
  panopto) shift; cmd_panopto "$@" ;;
  help|-h|--help)
    grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    echo "Unknown command: $1"
    echo "Try: $0 help"
    exit 1
    ;;
esac
