#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
tail -f "$SCRIPT_DIR/bot.log"
