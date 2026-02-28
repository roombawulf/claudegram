#!/bin/bash
set -euo pipefail

DEFAULT_REPO="https://github.com/roombawulf/claudegram.git"
REPO_URL="${1:-$DEFAULT_REPO}"
INSTALL_DIR="/opt/claudegram"
SERVICE_USER="claude-bot"
WORKSPACE_DIR="/home/${SERVICE_USER}/claude-workspace"
SERVICE_NAME="claudegram"

# ─── Colors ───
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[*]${NC} $1"; }
ok()    { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
fail()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }

# ─── Preflight ───
echo ""
echo -e "${CYAN}══════════════════════════════════════${NC}"
echo -e "${CYAN}       Claudegram — Installer           ${NC}"
echo -e "${CYAN}══════════════════════════════════════${NC}"
echo ""

[ "$(id -u)" -ne 0 ] && fail "Run as root: sudo ./install.sh (or curl | sudo bash)"

# Check for python3
if ! command -v python3 &>/dev/null; then
    fail "python3 not found. Install Python 3.10+ and re-run."
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Found Python $PYTHON_VERSION"

# Check for git
if ! command -v git &>/dev/null; then
    fail "git not found. Install git and re-run."
fi

# ─── Collect credentials ───
echo ""
echo -e "${YELLOW}You'll need three things:${NC}"
echo "  1. Telegram Bot Token  — from @BotFather"
echo "  2. Anthropic API Key   — from console.anthropic.com"
echo "  3. Your Telegram User ID — from @userinfobot"
echo ""

read -rp "Telegram Bot Token: " TELEGRAM_TOKEN < /dev/tty
[ -z "$TELEGRAM_TOKEN" ] && fail "Token cannot be empty"

read -rp "Anthropic API Key: " ANTHROPIC_KEY < /dev/tty
[ -z "$ANTHROPIC_KEY" ] && fail "API key cannot be empty"

read -rp "Your Telegram User ID (comma-separated for multiple): " USER_IDS < /dev/tty
[ -z "$USER_IDS" ] && fail "User ID cannot be empty"

# ─── Create service user ───
echo ""
if id "$SERVICE_USER" &>/dev/null; then
    ok "Service user '$SERVICE_USER' already exists"
else
    info "Creating service user: $SERVICE_USER"
    useradd -r -m -s /bin/bash "$SERVICE_USER"
    ok "Created user '$SERVICE_USER'"
fi

# ─── Clone or update repo ───
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Repo already cloned, pulling latest..."
    cd "$INSTALL_DIR"
    sudo -u "$SERVICE_USER" git pull --ff-only
    ok "Updated to latest"
else
    if [ -d "$INSTALL_DIR" ]; then
        warn "Directory $INSTALL_DIR exists but isn't a git repo — backing up"
        mv "$INSTALL_DIR" "${INSTALL_DIR}.bak.$(date +%s)"
    fi
    info "Cloning repo..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    ok "Cloned to $INSTALL_DIR"
fi

# ─── Python venv ───
info "Setting up Python virtual environment..."
if [ ! -d "$INSTALL_DIR/venv" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q
ok "Dependencies installed"

# ─── Workspace ───
info "Creating workspace: $WORKSPACE_DIR"
mkdir -p "$WORKSPACE_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$WORKSPACE_DIR"
ok "Workspace ready"

# ─── Data directory ───
mkdir -p "$INSTALL_DIR/data"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/data"

# ─── Write .env ───
info "Writing .env..."
cat > "$INSTALL_DIR/.env" <<EOF
# Core
TELEGRAM_BOT_TOKEN=$TELEGRAM_TOKEN
ANTHROPIC_API_KEY=$ANTHROPIC_KEY
ALLOWED_USER_IDS=$USER_IDS

# Directories
WORKSPACE_DIR=$WORKSPACE_DIR
DB_PATH=$INSTALL_DIR/data/bot.db
BOT_SOURCE_DIR=$INSTALL_DIR

# Models
SONNET_MODEL=claude-sonnet-4-6
HAIKU_MODEL=claude-haiku-4-5-20251001

# Streaming
STREAM_EDIT_INTERVAL_MS=1500
STREAM_MIN_CHARS=50

# Cost
DAILY_COST_ALERT_USD=5.0
EOF
chmod 600 "$INSTALL_DIR/.env"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
ok ".env written (permissions: 600)"

# ─── Sudoers for restart ───
SUDOERS_FILE="/etc/sudoers.d/claudegram"
if [ ! -f "$SUDOERS_FILE" ]; then
    info "Granting $SERVICE_USER passwordless restart permission..."
    echo "$SERVICE_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart $SERVICE_NAME" > "$SUDOERS_FILE"
    chmod 440 "$SUDOERS_FILE"
    ok "Sudoers configured (restart only)"
else
    ok "Sudoers already configured"
fi

# ─── Git config for the service user ───
info "Configuring git for $SERVICE_USER..."
sudo -u "$SERVICE_USER" git config --global user.name "claude-bot"
sudo -u "$SERVICE_USER" git config --global user.email "claude-bot@$(hostname)"
ok "Git identity set"

# ─── Systemd service ───
info "Installing systemd service..."
cp "$INSTALL_DIR/claudegram.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" --quiet
ok "Service enabled"

# ─── Start ───
echo ""
read -rp "Start the bot now? [Y/n] " START_NOW < /dev/tty
START_NOW=${START_NOW:-Y}

if [[ "$START_NOW" =~ ^[Yy]$ ]]; then
    systemctl start "$SERVICE_NAME"
    sleep 2
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        ok "Bot is running!"
    else
        warn "Bot may have failed to start. Check logs:"
        echo "  sudo journalctl -u $SERVICE_NAME -n 20 --no-pager"
    fi
else
    info "Start manually with: sudo systemctl start $SERVICE_NAME"
fi

# ─── Done ───
echo ""
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo -e "${GREEN}       Installation Complete!         ${NC}"
echo -e "${GREEN}══════════════════════════════════════${NC}"
echo ""
echo "  Manage:"
echo "    sudo systemctl status $SERVICE_NAME"
echo "    sudo systemctl restart $SERVICE_NAME"
echo "    sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "  Update:"
echo "    cd $INSTALL_DIR && sudo -u $SERVICE_USER git pull"
echo "    sudo systemctl restart $SERVICE_NAME"
echo ""
echo "  Config:  $INSTALL_DIR/.env"
echo "  Data:    $INSTALL_DIR/data/bot.db"
echo "  Memory:  $WORKSPACE_DIR/memory.json"
echo ""
