#!/bin/bash
set -euo pipefail

INSTALL_DIR="/opt/claude-telegram"
SERVICE_USER="claude-bot"
WORKSPACE_DIR="/home/${SERVICE_USER}/claude-workspace"

echo "=== Claude Telegram Bot Installer ==="
echo ""

# Check root
if [ "$(id -u)" -ne 0 ]; then
    echo "Error: Run as root (sudo ./install.sh)"
    exit 1
fi

# Create service user
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Creating service user: $SERVICE_USER"
    useradd -r -m -s /bin/bash "$SERVICE_USER"
fi

# Create workspace
echo "Creating workspace: $WORKSPACE_DIR"
mkdir -p "$WORKSPACE_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$WORKSPACE_DIR"

# Copy project files
echo "Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp -r bot/ requirements.txt "$INSTALL_DIR/"

# Set up Python venv
echo "Setting up Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

# Set up .env if not exists
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "Creating .env from template..."
    cp .env.example "$INSTALL_DIR/.env"
    # Set workspace dir
    sed -i "s|WORKSPACE_DIR=.*|WORKSPACE_DIR=$WORKSPACE_DIR|" "$INSTALL_DIR/.env"
    sed -i "s|DB_PATH=.*|DB_PATH=$INSTALL_DIR/data/bot.db|" "$INSTALL_DIR/.env"
    echo ""
    echo ">>> IMPORTANT: Edit $INSTALL_DIR/.env with your API keys <<<"
    echo ""
fi

# Create data directory
mkdir -p "$INSTALL_DIR/data"

# Set permissions
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# Install systemd service
echo "Installing systemd service..."
cp claude-telegram.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable claude-telegram

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit API keys:    sudo nano $INSTALL_DIR/.env"
echo "  2. Start the bot:    sudo systemctl start claude-telegram"
echo "  3. Check status:     sudo systemctl status claude-telegram"
echo "  4. View logs:        sudo journalctl -u claude-telegram -f"
echo ""
