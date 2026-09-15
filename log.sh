#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
tail -n 200 -f "$SCRIPT_DIR/bot.log"
