#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Check python3
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found."
    echo "Install it with:"
    echo "  sudo apt update && sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

echo "=== Gandalf Bot Setup ==="
echo ""

# Discord token
read -p "Discord bot token: " DISCORD_TOKEN
if [ -z "$DISCORD_TOKEN" ]; then
    echo "Error: token is required."
    exit 1
fi

# Allowed role
read -p "Allowed role name (leave blank to allow all): " ALLOWED_ROLE

# Script timeout
read -p "Script timeout in seconds [60]: " SCRIPT_TIMEOUT
SCRIPT_TIMEOUT=${SCRIPT_TIMEOUT:-60}

# Write .env
cat > "$SCRIPT_DIR/.env" <<EOF
DISCORD_TOKEN=$DISCORD_TOKEN
ALLOWED_ROLE=$ALLOWED_ROLE
SCRIPT_TIMEOUT=$SCRIPT_TIMEOUT
EOF

echo ""
echo ".env created."

# Create virtual environment
echo "Creating virtual environment..."
python3 -m venv "$SCRIPT_DIR/venv"

# Install requirements
echo "Installing Python dependencies..."
"$SCRIPT_DIR/venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "Setup complete. Run ./run.sh to start the bot."
