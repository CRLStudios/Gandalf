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
    # Wait for graceful shutdown (gateway close)
    for i in $(seq 1 10); do
        if ! kill -0 "$PID" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    # Force kill if still alive
    if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID"
        echo "Bot force-killed (PID $PID)."
    else
        echo "Bot stopped (PID $PID)."
    fi
    rm -f "$PIDFILE"
else
    rm -f "$PIDFILE"
    echo "Bot was not running (stale pidfile removed)."
fi
