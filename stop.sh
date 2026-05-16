#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$SCRIPT_DIR/bot.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "No pidfile found. Bot is not running."
    exit 1
fi

PID=$(cat "$PIDFILE")

if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    rm -f "$PIDFILE"
    echo "Bot stopped (PID $PID)."
else
    rm -f "$PIDFILE"
    echo "Bot was not running (stale pidfile removed)."
fi
