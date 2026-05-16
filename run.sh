#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$SCRIPT_DIR/bot.pid"
PYTHON="$SCRIPT_DIR/venv/bin/python3"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Bot is already running (PID $(cat "$PIDFILE"))."
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "Error: .env not found. Run ./setup.sh first."
    exit 1
fi

if [ ! -f "$PYTHON" ]; then
    echo "Error: venv not found. Run ./setup.sh first."
    exit 1
fi

echo "Starting Gandalf..."
nohup "$PYTHON" "$SCRIPT_DIR/bot.py" > "$SCRIPT_DIR/bot.log" 2>&1 &
echo $! > "$PIDFILE"
echo "Bot started (PID $(cat "$PIDFILE")). Logs: bot.log"
